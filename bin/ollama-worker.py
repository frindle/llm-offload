#!/usr/bin/env python3
"""Standalone agentic coding dispatch against Ollama's native /api/chat,
bypassing opencode's CLI entirely.

Built 2026-08-21 because opencode's CLI has two confirmed harness bugs
that make it unreliable for real dispatch:
  1. It corrupts the user-turn prompt before sending it to the provider
     (partial/broken quote-escaping) -- causally proven, 0/7 vs 6/6
     tool-call success with/without the corruption. Filed upstream:
     https://github.com/anomalyco/opencode/issues/43923
  2. A separate infinite self-nudge loop in its `build` agent
     ("Continue if you have next steps..."), confirmed model-independent.
Both bugs live inside opencode's compiled binary and don't exist if
opencode isn't in the dispatch path -- hence this script talks to Ollama
directly and implements its own minimal tool-execution loop.

Termination is based purely on "the model's response has no more
tool_calls" -- never on injecting a self-generated "continue" prompt --
which is the structural fix for bug #2 above.
"""
import argparse
import collections.abc
import difflib
import hashlib
import http.client  # mid-stream SSE failures surface as http.client exceptions
import html as html_module
import io
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys

# GIT_OPTIONAL_LOCKS=0 (2026-10-02, Rivian s5 566e7cc77629 / 5059223eacc3 / 454979f2a248):
# a plain `git status` REWRITES .git/index (it takes index.lock to refresh stat
# info). Our observers (worktree snapshots, dirty checks) run it with timeouts
# against .git dirs inside the iCloud Desktop repos, where it can be slow -- and a
# held or timeout-orphaned index.lock made auto-harness-check's `git checkout`
# fail with "index.lock: File exists" -> "HARNESS ERROR: git is unusable". With
# this set, read-only commands never take the optional lock; writes still lock.
os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

# iCloud "Optimize Storage" evicts the Desktop repos' .git metadata; without this a
# git child gets EDEADLK ("not a git repository: (null)") -- see dataless_policy.py.
try:
    import importlib.util as _dp_iu, os as _dp_os
    _dp_s = _dp_iu.spec_from_file_location("dataless_policy", _dp_os.path.join(
        _dp_os.path.dirname(_dp_os.path.realpath(__file__)), "dataless_policy.py"))
    _dp_m = _dp_iu.module_from_spec(_dp_s)
    _dp_s.loader.exec_module(_dp_m)
    _dp_m.enable()
except Exception:
    pass
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# model_profile: the model cards' sampling/thinking/max_tokens/ctx are the ONLY source of
# request defaults (model_profiles.yaml). CLI flags override only when explicitly given.
_HERE_DIR = os.path.dirname(os.path.realpath(__file__))
if _HERE_DIR not in sys.path:
    sys.path.insert(0, _HERE_DIR)
import model_profile as _mp
try:
    import worker_budget as _bb   # switchable budget policies (2026-10-09); every use is gated on a non-empty policy set
except Exception:                 # a missing/broken module must never break a default run
    _bb = None
try:
    import worker_levers as _wl   # opt-in convergence levers VERIFY_ON_EDIT / PROTECT_HARNESS / READ_MIN_WINDOW / PROSE_STICKY_NOTHINK (2026-10-10/11); default OFF except NOTHINK_RUN (default ON since 2026-10-15; NOTHINK_RUN=0 = off)
except Exception:                 # a missing/broken module must never break a default run
    _wl = None
import worker_local_guards as _lg   # 2026-10-13: repeated-search / local-redirect / verify-nudge guards (kill switch WORKER_LOCAL_GUARDS=0)
import worker_robust as _wr   # Phase 3 (2026-10-08): layered tool-call repair, format requery, loop detector, stop-gate
try:
    import plan_runtime as _pr   # 2026-10-14 hardening Wave 2: resolved-lane PLAN (SHADOW by default; WORKER_PLAN=0 / LANE_PLAN=0 = off). A broken module must never break a run.
except Exception:
    _pr = None
_LAST_OV = [()]    # body-field names the caller deliberately overrode on the most recent _profile_fields() (conformance `ov`)


def _conf_rec(host, path, api, model, payload, req, role, status, t0, raw=None, step=0):
    """One conformance record for one request on the wire (plan_runtime.rec; never raises, no-op without a
    queue launch / plan, CONFORMANCE_LOG=0 = off)."""
    if _pr is not None:
        _pr.rec(host=host, path=path, api=api, model=model, body=payload, req=req, role=role, status=status,
                t0=t0, raw=raw, step=step, ov=list(_LAST_OV[0]))

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

DEFAULT_MODEL = "qwen3-14b-agentic"
DEFAULT_HOST = "http://198.51.100.145:11434"
# --api openai's default when --host is omitted: Darkbloom's local OpenAI endpoint
# (2026-10-01; was the retired llama-server bypass on :8091). Read from
# ~/.darkbloom/local.json, which Darkbloom rewrites on every provider restart; the old
# :8091 is only a never-listening placeholder if Darkbloom isn't serving locally, so
# ensure_model_ready() fails with a clear "start Darkbloom" message. It must already be
# running, this script does not start it. pick_host() only knows the two native-Ollama
# hosts, so this is separate.
# Darkbloom 0.9.17 rewrites local.json with ONLY `api_key` (no `base_url`, seen
# 2026-10-04): reading base_url alone then meant "Darkbloom off" -- no Bearer key
# was sent (401s) and the queue routed Darkbloom-only jobs to Unraid. A record that
# carries an api_key IS the local endpoint being on, at the fixed local port
# (DARKBLOOM_BASE_URL overrides). Same rule as darkbloom_chat.base_from_record.
def _darkbloom_base_from_record(rec):
    """PURE. Endpoint root (no /v1): base_url, else the default local endpoint
    when the record carries an api_key, else None."""
    import os as _o
    rec = rec if isinstance(rec, dict) else {}
    b = str(rec.get("base_url") or "").strip().rstrip("/")
    if not b and rec.get("api_key"):
        b = (_o.environ.get("DARKBLOOM_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b if b.startswith("http") else None


def _darkbloom_local_record():
    try:
        import json as _j, os as _o
        rec = _j.load(open(_o.path.expanduser("~/.darkbloom/local.json")))
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _darkbloom_default_host():
    return _darkbloom_base_from_record(_darkbloom_local_record()) or "http://127.0.0.1:8091"
DEFAULT_OPENAI_HOST = _darkbloom_default_host()
# Models confirmed to crash on Ollama's native /api/chat (the "no user query
# found in messages" template bug, github.com/ollama/ollama/issues/17778) --
# these MUST go through the DEFAULT_OPENAI_HOST llama-server bypass. Added
# 2026-08-29 after a real double-load: a dispatch accidentally routed
# qwen3.8:27b-q8_0 through native Ollama on Studio (127.0.0.1:11434) while
# the dedicated bypass server for the exact same model was already resident
# on DEFAULT_OPENAI_HOST -- two ~35GB copies of the same weights in Studio's
# 64GB unified memory at once, which pushed swap to 13GB+ and left the
# bypass server wedged (every request 500'd with "Compute error") until both
# were killed and restarted. See ensure_model_ready()'s native-Ollama branch.
TEMPLATE_BUG_MODELS = {"qwen3.8:27b-q8_0", "devstral:24b"}
# DEFAULT_TEMPERATURE / DEFAULT_NUM_CTX were hardcoded here; the profile (model_profiles.yaml)
# now owns them. Kept only as the legacy values the profile FALLBACK documents.
DEFAULT_TEMPERATURE = 0.15   # legacy; NOT used as a default any more
DEFAULT_NUM_CTX = 16384      # legacy; NOT used as a default any more
DEFAULT_MAX_ITERS = 30  # raised 2026-08-28 from 20 -- now that --chat-timeout/--max-tokens/
                        # --repeat-penalty all bound the worst case PER iteration, a higher
                        # iteration ceiling no longer multiplies runaway risk the way it did
                        # before those existed; a real coding dispatch tonight (the live-log
                        # feature) used most of a 16-iteration budget for genuinely productive
                        # work, and a research task hit DID-NOT-CONVERGE at 15 while still
                        # making real (if unproductive) progress -- both suggest the old default
                        # was cutting off legitimate work, not just runaway loops.
# 60s was tight: a warm `npm run build` measures ~10-30s, but a large edit that
# invalidates the Turbopack cache can exceed it, and the model then receives a
# spurious "command timed out" that looks like its own code hanging. Cheap
# insurance -- a real hang still terminates, just later.
BASH_TIMEOUT_S = 240
# Per-turn tool-call fan-out cap. A model that emits dozens of (usually identical,
# blind) tool calls in a SINGLE assistant turn is not doing real work -- command-r
# fired 73 identical web_search calls in one turn, each returning nothing, then
# fabricated. Truncate a turn's tool_calls to this many so one runaway turn can't
# burn the whole budget / hammer a search backend; the model still gets every
# result it can act on and re-issues anything genuinely needed on the next turn.
MAX_TOOL_CALLS_PER_TURN = 12
WEB_TIMEOUT_S = 20
# Per-page cap for read_file. In CHARS, mirroring WEB_FETCH_MAX_CHARS, because a
# char budget is token-agnostic and needs no tokenizer. 16000 chars is ~4K
# tokens. Additionally clamped at call time to ~1/8 of the context window
# (num_ctx * 4 // 8 chars) so a small-ctx dispatch cannot spend an eighth of its
# window on a single read -- the cap that matters is whichever is SMALLER.
READ_FILE_MAX_CHARS = 16000
# budget policy `read_window`: when [0] > 0, read_file pages show AT LEAST this many lines (default
# when `length` is omitted/0, floor when it is smaller). 0 = off = historical behaviour.
_READ_WINDOW = [0]

# Pre-send ceiling, as a fraction of num_ctx. Deliberately ABOVE
# CONTEXT_REVIEW_THRESHOLD: this is the last-resort "this request will not fit"
# guard, and the review threshold should normally pause first, on real reported
# usage rather than on a bytes/4 estimate.
PRESEND_CONTEXT_LIMIT = 0.95

WEB_FETCH_MAX_CHARS = 5000  # lowered from 8000 2026-08-29 -- confirmed live that a real
                            # multi-document research task (2-3 full tariff PDFs) on
                            # Unraid's 12GB card compounds across iterations past even a
                            # carefully calibrated --num-ctx ceiling (started at ~12.5K
                            # tokens with real headroom under 14336, grew past it within
                            # 3 more iterations of fetching). 5000 chars (~1250 tokens) is
                            # still a substantial excerpt; the model can always fetch again
                            # for a specific detail it's missing.
ITERATION_LOW_BUDGET_THRESHOLD = 3  # added 2026-08-29, paired with the per-turn budget note in
# the main loop below: once <= this many iterations remain, the harness appends an explicit
# user-turn reminder that request_more_iterations exists, instead of only a passive count. Kept
# separate from CONTEXT_REVIEW_THRESHOLD (a fraction) because this is a small absolute count --
# the two review gates are independent and can trip at different times.

CONTEXT_REVIEW_THRESHOLD = 0.90  # added 2026-08-28 (the owner's request, "can we do the same for
# context?" -- paired with request_more_iterations): fraction of --num-ctx at which a dispatch
# pauses for review (same clean-stop-and-resume shape as the iteration request) rather than
# risk an actual context overflow or a silently degraded response near the ceiling.
# Proactive mid-run budget nudges (the owner 2026-09-08): the 0.90 pause above is post-hoc -- by
# the time it fires the run is already walled. These earlier thresholds warn the model to
# CONVERGE while it still has room, each firing at most once per run. Kept strictly below
# CONTEXT_REVIEW_THRESHOLD so the pause always supersedes the top nudge.
CONTEXT_NUDGE_THRESHOLDS = (0.60, 0.78)
DEFAULT_SEARXNG_HOST = "http://203.0.113.154:8080"
LOG_DIR = Path.home() / "bin" / "ollama-worker-logs"
UNRAID_OLLAMA_HOSTS = ("198.51.100.145",)  # substrings matched against --host to detect "this dispatch targets Unraid"


def _unraid_host_substrings() -> tuple:
    """UNRAID_OLLAMA_HOSTS plus the hostname of whatever the host table
    currently calls SMALL_HOST_NAME, so editing that host's URL in the config
    (or the dashboard Settings panel) can't silently desync the "this dispatch
    targets the small card" checks from the URL actually dispatched to."""
    extra = ()
    try:
        url = host_url(SMALL_HOST_NAME)
        if url:
            hn = urllib.parse.urlsplit(url).hostname
            if hn:
                extra = (hn,)
    except Exception:
        pass
    return tuple(dict.fromkeys(UNRAID_OLLAMA_HOSTS + extra))

# Host auto-selection -- added 2026-08-28 after dispatching qwen3.8:27b-q8_0
# (~30.7GB) to Unraid by relying on DEFAULT_HOST above without checking fit:
# confirmed via /api/ps that only ~9.3GB landed in the 3080's VRAM (12GB
# card) and ~21.4GB (70% of the model) spilled into system RAM, running
# mostly CPU-bound -- a 386s cold load. Two real hosts, two very different
# memory shapes: Unraid has a small dedicated GPU (fast, but VRAM-limited),
# the Mac Studio has 64GB of unified memory (no VRAM/RAM split, Metal treats
# it as one pool). Picking the host should be driven by whether the model
# actually fits the GPU, not by a single hardcoded default.
#
# CONFIGURABLE (2026-09-26): the host table used to be this literal dict, which
# hardwired the whole toolchain to the owner's own two boxes by IP. It now lives in
# ~/.ollama-dispatch/hosts.json, following the SAME convention as the other
# edit-without-a-code-change config in this toolchain (gate-on-complete.py's
# ~/.ollama-dispatch/model-ladder.json, ~/.ollama-dispatch/defaults.json):
# OLLAMA_DISPATCH_HOME-rooted, plain JSON, read at USE time (not cached at
# import) so an edit -- by hand or from the dashboard's Settings panel -- takes
# effect on the next dispatch decision with no restart. On first run with no
# file present the seed below is written out verbatim, so an existing install
# keeps behaving identically.
#
# Schema: {"hosts": {"<name>": {"url": "http://host:11434",
#                               "usable_bytes": <int> | null}}}
# usable_bytes null/absent == UNMEASURED, which is deliberately NOT "assumed
# safe": the static pre-dispatch fit gate cannot clear such a host, exactly the
# way a model with no UNRAID_SPILLOVER_EXCEPTIONS entry is unmeasured rather
# than safe, and the live post-warmup spillover check in ensure_model_ready()
# remains the real backstop.
DISPATCH_HOME = Path(os.environ.get("OLLAMA_DISPATCH_HOME",
                                    str(Path.home() / ".ollama-dispatch"))).expanduser()
OLLAMA_HOSTS_FILE = DISPATCH_HOME / "hosts.json"
SEEDED_OLLAMA_HOSTS = {
    "unraid": {
        "url": "http://198.51.100.145:11434",
        # 3080, 12GB VRAM (confirmed 2026-08-27). Reserve ~20% for KV
        # cache/context overhead so a model that just barely fits the raw
        # VRAM figure doesn't still spill once a real context is loaded.
        "usable_bytes": int(12 * 1024**3 * 0.8),
    },
    "studio": {
        "url": "http://127.0.0.1:11434",
        # 64GB unified memory (confirmed live via `sysctl hw.memsize` on
        # mac-host). Reserve ~20GB for the OS and whatever else is
        # running rather than the full 64GB.
        "usable_bytes": 44 * 1024**3,
    },
}
# The two names the auto-routing logic below still treats specially when they
# exist: BIG is the preferred/overflow-safe host, SMALL the VRAM-limited card.
# Absent from the config -> those branches simply don't apply (a packaged
# install with one host named something else routes by fit, not by name).
BIG_HOST_NAME = "studio"
SMALL_HOST_NAME = "unraid"
_HOSTS_RELOAD_MIN_INTERVAL_S = 1.0
_hosts_cache = {"hosts": None, "mtime": None, "checked": 0.0}


def _normalize_ollama_hosts(raw) -> dict:
    """PURE. Coerce a raw hosts mapping into {name: {"url": str,
    "usable_bytes": int|None}}. Drops entries with no usable name/url; a
    missing, null, non-numeric or non-positive usable_bytes becomes None
    (UNMEASURED -- see the schema note above). Never raises."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, spec in raw.items():
        name = str(name or "").strip()
        if not name:
            continue
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict):
            continue
        url = str(spec.get("url") or "").strip().rstrip("/")
        if not url.startswith("http"):
            continue
        ub = spec.get("usable_bytes")
        try:
            ub = int(ub)
            if ub <= 0:
                ub = None
        except (TypeError, ValueError):
            ub = None
        out[name] = {"url": url, "usable_bytes": ub}
    return out


def _write_ollama_hosts_file(hosts: dict) -> None:
    """Persist `hosts` to OLLAMA_HOSTS_FILE atomically (write-temp-then-rename,
    so a concurrent reader never sees a half-written file)."""
    OLLAMA_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OLLAMA_HOSTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"hosts": hosts}, indent=2) + "\n")
    tmp.replace(OLLAMA_HOSTS_FILE)


def load_ollama_hosts(force: bool = False) -> dict:
    """The ACTIVE host table, resolved at use time. Reads OLLAMA_HOSTS_FILE
    (re-reading whenever its mtime changed, so a dashboard/hand edit is live
    without a restart); seeds the file from SEEDED_OLLAMA_HOSTS on first run;
    fails open to the seed if the file is missing or garbage rather than
    leaving the dispatcher with no hosts at all."""
    now = time.monotonic()
    if not force and _hosts_cache["hosts"] is not None \
            and now - _hosts_cache["checked"] < _HOSTS_RELOAD_MIN_INTERVAL_S:
        return _hosts_cache["hosts"]
    _hosts_cache["checked"] = now
    try:
        mtime = OLLAMA_HOSTS_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime is None:
        # First run (or the file was deleted): seed it with the current
        # defaults so this install keeps working identically, and so the file
        # is there to be edited.
        try:
            _write_ollama_hosts_file(SEEDED_OLLAMA_HOSTS)
            mtime = OLLAMA_HOSTS_FILE.stat().st_mtime_ns
        except OSError:
            _hosts_cache.update(hosts=dict(SEEDED_OLLAMA_HOSTS), mtime=None)
            return _hosts_cache["hosts"]
    if not force and _hosts_cache["hosts"] is not None and mtime == _hosts_cache["mtime"]:
        return _hosts_cache["hosts"]
    hosts = None
    try:
        d = json.loads(OLLAMA_HOSTS_FILE.read_text())
        raw = d.get("hosts") if isinstance(d, dict) and "hosts" in d else d
        hosts = _normalize_ollama_hosts(raw)
    except Exception:
        hosts = None
    if not hosts:
        hosts = _normalize_ollama_hosts(SEEDED_OLLAMA_HOSTS)
    _hosts_cache.update(hosts=hosts, mtime=mtime)
    return hosts


def save_ollama_hosts(hosts: dict) -> dict:
    """Validate + persist a new host table and return what was actually
    written. Refuses to write an EMPTY table: a dispatcher with zero hosts
    can't run anything, and silently emptying it from a UI misclick would be
    a far worse failure than rejecting the edit."""
    norm = _normalize_ollama_hosts(hosts)
    if not norm:
        raise ValueError("refusing to save an empty host table -- at least one "
                         "host with a valid http(s) url is required")
    _write_ollama_hosts_file(norm)
    load_ollama_hosts(force=True)
    return norm


def host_url(name: str, default=None):
    """URL of the configured host called `name`, or `default` if it isn't
    configured. Use this instead of KNOWN_OLLAMA_HOSTS[name]["url"] anywhere
    the name might legitimately be absent (the table is user-editable now)."""
    spec = load_ollama_hosts().get(name)
    return spec["url"] if spec else default


def host_usable_bytes(name: str):
    """usable_bytes for the configured host called `name`, or None when the
    host isn't configured OR is configured without a budget (UNMEASURED --
    callers must treat None as 'cannot prove a fit', never as 'fits')."""
    spec = load_ollama_hosts().get(name)
    return spec["usable_bytes"] if spec else None


class _LiveOllamaHosts(collections.abc.Mapping):
    """Read-only Mapping view over load_ollama_hosts(), so every historical
    `KNOWN_OLLAMA_HOSTS[name]["url"]` / `.items()` / `.values()` call site
    keeps working unchanged AND picks up config edits live, instead of
    freezing whatever the table looked like at import time."""

    def __getitem__(self, key):
        return load_ollama_hosts()[key]

    def __iter__(self):
        return iter(load_ollama_hosts())

    def __len__(self):
        return len(load_ollama_hosts())

    def __repr__(self):
        return f"_LiveOllamaHosts({dict(load_ollama_hosts())!r})"


KNOWN_OLLAMA_HOSTS = _LiveOllamaHosts()

# Confirmed live 2026-08-28 (measured via /api/ps: size == size_vram, zero
# spillover) -- see Agent-Dispatch-Log.md "Unraid VRAM math now fully
# mapped". A hard cap, not a suggestion: Unraid's spillover check in
# ensure_model_ready() already aborts on ANY spillover, but only AFTER a
# real warmup load -- up to several minutes wasted finding out the hard way.
# This catches it before dispatch even starts. The owner's call 2026-08-28
# ("can we code that in as a hard cap that will catch prior to dispatch?"):
# clamp down and log loudly, don't silently proceed and don't just warn.
# A model with no entry here is simply unmeasured, not assumed safe --
# ensure_model_ready()'s live post-warmup check remains the real backstop
# for anything not in this table yet.
UNRAID_SPILLOVER_EXCEPTIONS = {
    # The blanket "ANY spillover aborts" rule below exists for DENSE-model
    # spillover, which is genuinely bad (every layer touches every token, so
    # CPU-resident layers tax every forward pass). MoE models are a different
    # case: active-param sparsity means CPU-offloaded experts are only
    # touched when routed to, not every token -- confirmed live 2026-08-29,
    # gpt-oss:20b loaded at 10.96GB VRAM / 3.46GB CPU (24% spilled) and still
    # ran ~41 tok/s on a short response, competitive with several fully-
    # on-GPU dense models tested the same night. The owner's call: document real
    # per-model exceptions here rather than loosen the rule generally.
    "gpt-oss:20b": {"max_spill_frac": 0.30},
}

UNRAID_CONFIRMED_SAFE_CTX = {
    # qwen3.5:9b/qwen3:8b/llama3.1:8b lowered 14336 -> 12288 on 2026-08-29 after a
    # real, reproducible CUDA OOM (Fable's diagnosis): qwen3.5:9b at the OLD 14336
    # value succeeded on iteration 1 of a multi-turn agentic dispatch, then failed
    # identically on iteration 2's generation call, 3/3 attempts, same settings, same
    # already-resident model. 14336 was validated against a single-shot prompt size;
    # llama.cpp's CUDA compute buffers scale with actual prompt/batch size, not just
    # num_ctx, and iteration 2+ of a real agentic session carries iteration 1's full
    # output forward -- a strictly bigger prompt than whatever validated this number
    # originally, landing right at the 3080's 12GB margin. Lowered uniformly for the
    # other two same-size dense models sharing this value, not just the one that
    # actually failed -- same GPU, same failure mechanism, same risk. qwen3:14b's
    # 6144 is untouched (different size class, no observed failure at that value yet).
    "qwen3.5:9b": 12288,
    "qwen3:8b": 12288,
    "llama3.1:8b": 12288,
    "qwen3:14b": 6144,
    # gpt-oss:20b: base weights alone (13.79GB) exceed Unraid's 10.3GB
    # static usable-VRAM gate -- no --num-ctx value fixes this, it needs
    # Studio instead. Deliberately has no entry: there is no safe number to
    # clamp to, the model just doesn't belong on this host at all.
}


def clamp_unraid_ctx(host: str, model: str, num_ctx: int) -> int:
    """Hard cap: if `host` is Unraid and `model` has a confirmed-safe ceiling
    on record, clamp num_ctx down to it and log loudly. Returns the
    (possibly unchanged) num_ctx to actually use."""
    # host_url(): the small-card host is user-configurable now and may be
    # absent entirely, in which case there is no Unraid to clamp for.
    if host != host_url(SMALL_HOST_NAME):
        return num_ctx
    safe = UNRAID_CONFIRMED_SAFE_CTX.get(model)
    if safe is not None and num_ctx > safe:
        log(f"[worker] HARD CAP: {model} on Unraid requested --num-ctx {num_ctx}, but "
            f"confirmed-safe ceiling is {safe} (measured live, zero spillover) -- clamping "
            f"down instead of wasting a warmup-then-abort cycle finding this out the hard way.")
        return safe
    return num_ctx
LAN_MOUNT_ROOT = "/Volumes/data"  # Unraid's SMB share, mounted here when available
COPY_HELPER = str(Path.home() / "bin" / "copy-ollama-model-from-unraid.py")

# Local hot cache: OLLAMA_MODELS normally points here (fast NVMe). The
# shared SMB store (mounted for both this Mac and Unraid) is the source of
# truth / cold storage -- confirmed live 2026-08-21 that Ollama's own
# model-load path over SMB hangs indefinitely regardless of mmap setting,
# while a plain file copy from the same share does not, so copying once
# and loading locally is the actual fix, not a network/client tuning one.
# Ollama's REAL model store on this Mac. Was `~/ollama-models-local` until
# 2026-08-22, which nothing ever read: OLLAMA_MODELS is unset in the running
# `ollama serve` process, so Ollama loads from its default `~/.ollama/models`.
#
# The consequence was a double SMB transfer for every model not already local:
# ensure_model_cached() copied blobs+manifest from the share into
# ~/ollama-models-local (invisible to Ollama), ensure_model_ready() then found
# the model still missing from /api/tags and ran copy-ollama-model-from-unraid.py,
# which copies from the SAME share again into ~/.ollama/models (its LOCAL_ROOT).
# That second copy is the one that ever worked; the first just accumulated --
# 83GB of it, across 19 blobs, none of them unique.
#
# Pointing this at the real store makes the first copy the only copy: blobs and
# manifest land where Ollama reads, so the model is visible immediately and the
# helper's per-blob "SKIP (already at destination)" makes the second pass free.
LOCAL_MODEL_CACHE = Path.home() / ".ollama" / "models"
SMB_MODEL_SOURCE = Path("/Volumes/data/ollama-models")
MODEL_PULL_LOG = Path.home() / "bin" / "ollama-model-pulls.log"
# Vault REST default (Basic Memory behind vault-shim). _vault_endpoint() resolves the URL and the
# token per call through vault_conf.py, so nothing reads the token at import.
VAULT_URL_DEFAULT = "http://192.0.2.240:27123"
# Unified dispatch ledger (2026-09-14): queue/qwen dispatches and Agent-tool
# dispatches now share ONE note so the whole offload stream is in one place. This
# used to point at "Claude/Ollama/Ollama-Dispatch-Log.md" (queue-only), while the
# Agent-tool hook logged to Agent-Dispatch-Log.md -- two split ledgers. Repointed to
# the combined note; each worker line now carries a `[queue]` source tag (see
# log_dispatch_to_vault) so the two streams stay distinguishable.
#
# ROTATION (2026-09-27): the single unbounded Agent-Dispatch-Log.md grew to 4.9MB
# and OOM-crashed the old vault app's renderer on vault-open (its per-block/link/tag
# metadata index ballooned past the ~4GB V8 heap before first paint). The old
# file is quarantined out of the vault. The log now ROTATES MONTHLY so no single
# note can accumulate past that ceiling again.
VAULT_DISPATCH_LOG_BASE = "Claude/Agent-Dispatch-Log"


def _vault_endpoint():
    """(base_url, token) for the vault REST (Basic Memory behind vault-shim).
    Order: env VAULT_URL / VAULT_TOKEN_FILE -> ~/.config/vault/env -> vault_conf's defaults
    (VAULT_URL_DEFAULT for the URL). If vault_conf.py is missing: the default URL and no token,
    so the dispatch log is skipped rather than failing the dispatch."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import vault_conf
        return vault_conf.vault_url(VAULT_URL_DEFAULT), vault_conf.vault_token()
    except Exception:
        return VAULT_URL_DEFAULT, ""


def vault_dispatch_log_path() -> str:
    """Monthly-rotated dispatch-log path, e.g. Claude/Agent-Dispatch-Log-2026-09.md.
    Prevents the unbounded-single-file vault OOM found 2026-09-27."""
    return f"{VAULT_DISPATCH_LOG_BASE}-{datetime.now(timezone.utc):%Y-%m}.md"

SYSTEM_PROMPT = """You are a focused coding agent. You have ten tools: \
list_files, read_file, write_file, edit_file, run_bash, web_search, \
web_fetch, request_diagnostics, request_more_iterations, and task_complete. \
When you need evidence (recent commits, a diff, a grep, a file window, the \
verify output, container logs), call request_diagnostics with a list of \
requests before reaching for run_bash -- it is bounded and read-only, so it \
cannot damage the tree. Use them to \
accomplish the task directly -- don't describe what you would do, actually \
call the tools.

You are running under a finite iteration budget, and the harness keeps \
you informed of it: after each iteration's tool results it appends a \
short note of the form "[Iteration i/N: R remaining in your budget.]", \
and when few iterations remain it says so explicitly. Track your own \
progress against that budget as you go -- if you can see concrete \
remaining work that will not fit in the iterations left, call \
request_more_iterations now with a specific reason and how many more \
you need. Use exactly the standard its description sets: only for real \
remaining work you can point to -- never as a routine check-in, and \
never to recover from being stuck (fix the actual problem instead). It \
pauses the session for review rather than granting anything immediately; \
if the task is actually done, call task_complete.

ALWAYS start by calling list_files on '.' to see the project's real \
structure, then read the files that matter, BEFORE writing anything. Never \
guess a file path, and never assume a framework or directory layout -- \
discover it. The project's existing stack and conventions are whatever \
list_files and read_file actually show you, not what a project like this \
usually looks like. Prefer edit_file over write_file when changing an \
existing file.

If AGENTS.md, CLAUDE.md, README.md or CONTRIBUTING.md exist at the top \
level, read them before writing any code. They are written for you and \
state this project's conventions, which may deliberately differ from what \
you learned in training. Follow them over your own habits.

Before you CREATE a new file, read an existing file of the same kind in \
this project and match its idiom exactly -- its imports, its export style, \
its function signatures, its naming. A framework often has several valid \
styles from different versions; the only one that works here is the one \
already in use. If you are adding an API route, read an existing API route \
first. If you are adding a module or target, read how existing ones are \
declared. Your training data is likely to be older than this project.

web_search/web_fetch are for external documentation only (an unfamiliar \
third-party API). Never use them to learn about the project in front of \
you -- that is what list_files and read_file are for.

If you import or require a package, make sure it is actually a dependency \
of this project first -- check package.json (or the equivalent manifest) \
with read_file, and if it is missing either install it with run_bash or \
use something already available. Code that imports a package the project \
does not have will fail to build.

If a path you tried does not exist, do not try it again. Call list_files \
to find where the file actually is, and only use paths you have seen in a \
list_files result.

When the task is fully complete, call task_complete with a short summary. \
(A plain-text response with no further tool calls also ends the session, \
but task_complete is preferred -- it lets the harness confirm your work \
against the task's verification command before accepting it, instead of \
finding out only afterward.) Keep file paths relative to the working \
directory. Do not ask clarifying questions; make reasonable assumptions \
and proceed."""

RESEARCH_SYSTEM_PROMPT = """You are a focused research agent. You have two \
tools that matter for this task: web_search (self-hosted SearXNG) and \
web_fetch (reads the full content of a specific URL, including PDFs). You \
also have list_files/read_file/write_file/edit_file/run_bash available, but \
this is a research task, not a coding task -- there is no project to \
discover and nothing to build. Do not call list_files or read_file out of \
habit; there is nothing there to find unless the task itself gives you a \
reason to look.

A web_search result is a short snippet only -- a title, URL, and one or two \
truncated sentences. It is NEVER enough on its own to state a specific \
number, date, name, or any other concrete fact. For every specific claim in \
your final answer, you must have actually called web_fetch on a real source \
and read its content. If you catch yourself about to state a specific \
detail you only saw in a snippet, not in fetched page content, stop and \
fetch the source first, or say plainly in your final answer that you \
couldn't confirm it. A wrong-but-confident answer is worse than an honest \
"I couldn't verify this."

Use web_search multiple times with different, more specific queries if your \
first search doesn't turn up enough -- don't stop after one search just \
because it returned something.

When you have enough to answer the question, respond with your final answer \
as plain text and no further tool calls -- do not write it to a file unless \
the task explicitly asked for a file. Do not ask clarifying questions; make \
reasonable judgment calls about scope and proceed."""

# Added 2026-08-28 after confirming live (qwen3.5:9b) that reusing the
# coding SYSTEM_PROMPT for a research dispatch actively causes harm, not
# just irrelevance: "You are a focused coding agent... ALWAYS start by
# calling list_files on '.'" primed the model, on an EMPTY research scratch
# directory, to hallucinate an entire unrelated coding task (it wrote a
# Flask web server nobody asked for) rather than doing the research the
# task actually described. The whole rest of that prompt (matching existing
# code idiom, reading AGENTS.md/package.json, "Prefer edit_file over
# write_file") is equally irrelevant noise for a task with no project to
# read. This is a separate, more foundational fix than the corrective-nudge
# task_kind fix above -- that one only affects behavior once a no-tool-call
# response happens; this one shapes the model's understanding of what kind
# of task it's even doing from the very first token.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files and subdirectories at a path. Use this FIRST to discover the project's structure before reading or writing anything. Pass '.' for the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to the working directory. Use '.' for the working directory itself."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            # The SCHEMA is the fix, not just the implementation. This tool
            # previously declared `path` only and described itself as "Read a
            # file's full contents", while a model -- reasoning from other
            # harnesses -- called it with offset/length anyway. Those arguments
            # were silently dropped and the whole file came back: a 275KB read
            # against a 131K window, which paused a real dispatch at
            # 129,692/131,072 tokens on iteration 5. Describing the paging here
            # is what stops the prompt teaching the old behaviour.
            "description": (
                "Read a file. Large files come back in PAGES, not in full. "
                "Always pass all three parameters: `path`, `offset` (1 = first line) and "
                "`length` (0 = to the end of the file, still one page); if the file is "
                "bigger than one page the result ends with a notice giving the "
                "total line count and the exact next call to make. Pass "
                "`offset` (1-based line number to start at) and `length` "
                "(how many lines) to read a specific range -- that is how you "
                "reach the rest of a large file, and how you re-read around a "
                "specific region."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "offset": {"type": "integer",
                               "description": "1-based line number to start reading at. ALWAYS pass it: use 1 to start at the first line."},
                    "length": {"type": "integer",
                               "description": "How many lines to read. ALWAYS pass it: use 0 to read to the end of the file (still capped to one page)."},
                },
                # Every parameter is REQUIRED (2026-10-09, llama.cpp #20164: optional parameters make
                # Qwen3.5-35B / Qwen3-Coder loop on tool calls at long contexts). The defaults live in
                # the descriptions; a call that omits offset/length is still honoured (old transcripts,
                # weaker models) -- see worker_robust.DEFAULTED_PARAMS.
                "required": ["path", "offset", "length"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a NEW file, or fully overwrite an existing one, with the given content. Creates parent directories as needed. For an existing file where you're only changing part of it, use edit_file instead -- write_file forces you to regenerate the entire file from scratch in one response, which is slow and error-prone for anything but a small or brand-new file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_string with new_string in an existing file. Use this instead of write_file for any change to a file you didn't just create -- it only requires you to output the small changed region, not the whole file. old_string must match the file's current content exactly (including whitespace/indentation) and must be unique in the file; include enough surrounding context (a few lines before/after) to make it unique if the change itself is a short/common line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "old_string": {"type": "string", "description": "Exact existing text to replace, unique within the file."},
                    "new_string": {"type": "string", "description": "Text to replace it with."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the working directory and return stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (self-hosted SearXNG). Returns the top results as title/url/snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return its main readable text content (HTML stripped of nav/ads/scripts). Truncated if very long.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        # "get me these logs" loop (2026-09-03). Read-only, allowlisted,
        # capped, redacted -- executor lives in dispatch-diagnostics.py so it
        # can be tested and reasoned about without the worker. NOT a shell:
        # run_bash stays the (audited) escape hatch; this is the bounded path
        # a model should reach for first when it needs evidence.
        "type": "function",
        "function": {
            "name": "request_diagnostics",
            "description": (
                "Fetch read-only evidence to diagnose the task: git history/diff/blame, a line window "
                "of a file, a directory listing, a grep, this job's own verify output, or bounded "
                "`docker logs` from a catalogued container. Pass a LIST of requests (max 5 per call, "
                "6 calls per run). Kinds: git {args:[...]} (read subcommands only), file {path,start,end}, "
                "ls {path,depth}, grep {pattern,glob,regex}, verify {}, docker_logs {container,tail,since,filter}. "
                "Output is capped and secrets are redacted. Refusals come back with the reason -- read it, "
                "do not retry the same request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requests": {
                        "type": "array",
                        "description": "Request objects, each {kind: ..., ...that kind's fields}.",
                        "items": {"type": "object"},
                    },
                    "why": {"type": "string", "description": "One line: what this evidence will settle (pass an empty string if there is nothing to add)."},
                },
                "required": ["requests", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_more_iterations",
            "description": "Ask for more iterations if you're making real progress but won't "
                            "finish within your current budget. Only call this when you can "
                            "point to concrete remaining work -- not as a routine check-in, and "
                            "not to recover from being stuck (fix the actual problem instead). "
                            "This pauses the session for review rather than granting anything "
                            "immediately -- you will not get a response to this call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "additional": {"type": "integer",
                                   "description": "How many more iterations you're asking for."},
                    "reason": {"type": "string",
                               "description": "What concrete remaining work justifies this -- "
                                              "be specific."},
                },
                "required": ["additional", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Call this ONCE, when the task is fully complete, instead of just "
                            "stopping. Pass a short summary of what you did. If the task has a "
                            "verification command, the harness runs it before accepting: if it "
                            "fails, you get its output back as this tool's result and must fix "
                            "the reported problem(s) before calling task_complete again. Do not "
                            "call this if you know work remains -- and do not run the "
                            "verification command yourself, the harness already does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "What was done, which files changed, and how "
                                               "you know it works."},
                },
                "required": ["summary"],
            },
        },
    },
]


def render_manual_tools_block(tools: list) -> str:
    """Textual tool-schema injection for models whose Ollama chat template
    doesn't render the native `tools` API field into the prompt at all --
    confirmed 2026-08-21 for deepseek-r1 distills (incl. the community
    'MFDoom/deepseek-r1-tool-calling' build, which ships the exact same
    template gap): rendering the real Jinja template locally with `tools`
    populated proved it never appears in the output. Root cause per an
    Ollama maintainer (github.com/ollama/ollama/issues/8517): these distills
    don't emit the exact special-token sequence Ollama's native tool_calls
    parser requires. This sidesteps that parser entirely by describing the
    tools as plain text (Qwen/DeepSeek's own convention) and having
    extract_manual_tool_call() below parse the model's response ourselves."""
    lines = [json.dumps({"type": "function", "function": t.get("function", t)}) for t in tools]
    tools_xml = "\n".join(lines)
    valid_names = ", ".join(t.get("function", t)["name"] for t in tools)
    return f"""# Tools

You may call one function per response to assist with the user query. You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools_xml}
</tools>

For each function call, return ONLY a JSON object with function name and arguments within <tool_call></tool_call> XML tags -- nothing else, no other text:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>

CRITICAL: the "name" field must be EXACTLY one of these literal tool names:
  {valid_names}
Never put a file path, a filename, or anything else in "name" -- file paths always go inside "arguments".
Correct:   {{"name": "read_file", "arguments": {{"path": "app/page.tsx"}}}}
INCORRECT: {{"name": "app/page.tsx", "arguments": {{"path": "app"}}}}

When the task is fully done and no more function calls are needed, respond with a normal plain-text message and NOT a <tool_call> block."""


_VALID_JSON_ESCAPE_CHARS = set('"\\/bfnrtu')


def _repair_invalid_json_escapes(s: str) -> str:
    """Char-by-char (not regex-substitution) repair of a JSON-string
    candidate containing backslashes that aren't valid JSON escapes --
    e.g. a model copying a shell `find ... \\( -o ... \\)` snippet into a
    tool-call argument without doubling those backslashes for JSON.
    Confirmed live 2026-08-21 (qwen2.5-coder:14b) that a naive single-char
    lookahead regex (`re.sub(r'\\\\(?!...)', ...)`) double-counts an
    ALREADY-valid two-character escape like `\\.` (backslash-backslash
    then a literal dot) -- it inspects the second backslash of that valid
    pair in isolation, sees the dot after it, and wrongly "repairs" a
    correct escape. Walking left-to-right and consuming valid pairs whole
    avoids that: only a backslash NOT already paired with a valid escape
    char gets doubled, and the walk advances 2 chars over anything it
    correctly recognized as already-valid."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _VALID_JSON_ESCAPE_CHARS:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == "\\":
            out.append("\\\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_NUMERIC_CLAIM_RE = re.compile(
    r'\$\s?\d+(?:\.\d+)?\s*(?:/\s?k?Wh|per\s+k?Wh|/\s?kW\b)?'
    r'|\b\d{1,2}(?::\d{2})?\s?(?:AM|PM|am|pm)\b'
)


def find_ungrounded_numeric_claims(answer_text: str, source_text: str) -> list:
    """Extract dollar-amount and clock-time claims from a research answer and return
    any that don't appear (whitespace-normalized) in the source facts text.

    Confirmed live 2026-08-28 (qwen3:8b, NV-Energy facts-provided pass-2 run): a model
    can invent precise, confident-sounding rate figures ($0.25/kWh, specific hour
    ranges) with zero basis in the facts it was actually given, directly against an
    explicit "do not fabricate a number" instruction in the task text -- and nothing
    caught it, because the old fabrication nudge only checked whether a web_fetch had
    succeeded THIS session, and that check is disabled entirely in facts-provided mode
    (see run_task's facts_provided param) since zero fetches is the whole point there.
    This isn't a general fact-checker -- it only catches the two claim shapes that
    have shown up as confident fabrications in practice, and it will also flag a
    correctly-cited number if the source phrases it differently enough to fail a
    normalized substring match. Both are fine: it exists to force a second look before
    a model's answer is accepted, not to silently pass or fail it."""
    def norm(s):
        return re.sub(r'\s+', ' ', s).strip()
    source_norm = norm(source_text)
    claims = sorted(set(norm(m.group(0)) for m in _NUMERIC_CLAIM_RE.finditer(answer_text)))
    return [c for c in claims if c not in source_norm]


# Real tool names, used to validate the loosest tool-call dialect (the flat
# {"tool": NAME, ...} form) so a prose JSON object that merely carries a "tool"
# key is never mistaken for a call.
_VALID_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def _coerce_tool_call(obj):
    """Normalize one candidate JSON object into {"name":..., "arguments":{...}}
    if it is recognizably a tool call, else return None.

    Priority order:
      1. explicit {"name": NAME, "arguments"|"parameters": {...}} -- accepted for
         ANY name (unchanged behaviour: an unknown name is surfaced by the loop).
      2. flat {"tool": NAME, <arg-key>: <val>, ...} -- the devstral/Mistral fenced
         ```json dialect where the tool name is under "tool" and the arguments are
         sibling keys (NOT nested). Confirmed live 2026-09-09 (devstral:24b,
         transcript 20260909T021925Z.json). VALIDATED against real tool names to
         avoid prose false positives.
      3. neither -> None (a {"function": {...}} / {"tool_call": {...}} wrapper,
         which the caller rescans INTO)."""
    if not isinstance(obj, dict):
        return None

    def _as_dict(a):
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except Exception:
                return {}
        return a if isinstance(a, dict) else {}

    if isinstance(obj.get("name"), str) and ("arguments" in obj or "parameters" in obj):
        args = obj["arguments"] if "arguments" in obj else obj["parameters"]
        return {"name": obj["name"], "arguments": _as_dict(args)}

    tname = obj.get("tool")
    if not isinstance(tname, str):
        tname = obj.get("tool_name")
    if isinstance(tname, str) and tname.strip() in _VALID_TOOL_NAMES:
        if "arguments" in obj:
            args = _as_dict(obj["arguments"])
        elif "parameters" in obj:
            args = _as_dict(obj["parameters"])
        else:
            args = {k: v for k, v in obj.items() if k not in ("tool", "tool_name")}
        return {"name": tname.strip(), "arguments": args}

    return None


def _unwrap_tool_args(name, args, logfn=None):
    """Unwrap a Cohere/command-r NATIVE tool_calls argument envelope.

    Every other model emits a tool call's arguments FLAT -- web_search's args are
    {"query": ...}. Cohere-style models (command-r) instead wrap them:
        {"tool_name": "web_search", "parameters": {"query": ...}}
    so the flat readers (args.get("query")) found nothing and EVERY such call was
    rejected -- command-r's research fired dozens of blind web_search calls that all
    came back empty, then fabricated an answer with zero real searches.

    If `args` carries a nested `parameters` dict AND either a sibling `tool_name` or
    no other keys, unwrap `parameters` as the real args. When `tool_name` is present
    it is reconciled against the invoked function `name` (mismatch is logged; the
    invoked name is authoritative -- that is what the loop will actually dispatch).
    A well-formed flat arg dict (no `tool_name`, or `parameters` sitting alongside
    real sibling args) passes through UNCHANGED, so no other model regresses."""
    if not isinstance(args, dict):
        return args
    inner = args.get("parameters")
    if isinstance(inner, dict) and (
            "tool_name" in args or set(args.keys()) <= {"tool_name", "parameters"}):
        tn = args.get("tool_name")
        if (isinstance(tn, str) and tn.strip() and name and tn.strip() != name
                and logfn):
            logfn(f"[worker] command-r tool envelope: tool_name={tn.strip()!r} != "
                  f"invoked function {name!r} -- trusting the invoked name, "
                  f"unwrapping parameters")
        return inner
    return args


def _extract_tool_calls_token(content: str, calls: list) -> str:
    """Consume Mistral/devstral `[TOOL_CALLS]` payloads: the literal token
    followed by a JSON array (or single object) of calls. Appends recognized
    calls to `calls` and returns `content` with each consumed span blanked out
    (spaces of equal length) so the brace scanner below can't double-count."""
    if "[TOOL_CALLS]" not in content:
        return content
    out = content
    search_from = 0
    while True:
        idx = out.find("[TOOL_CALLS]", search_from)
        if idx < 0:
            break
        rest_start = idx + len("[TOOL_CALLS]")
        rest = out[rest_start:]
        stripped = rest.lstrip()
        lead = len(rest) - len(stripped)
        try:
            payload, consumed = json.JSONDecoder().raw_decode(stripped)
        except Exception:
            search_from = rest_start
            continue
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            c = _coerce_tool_call(it)
            if c is not None:
                calls.append(c)
        payload_end = rest_start + lead + consumed
        out = out[:idx] + (" " * (payload_end - idx)) + out[payload_end:]
        search_from = payload_end
    return out


def extract_manual_tool_calls(content: str) -> tuple:
    """Find every {"name": ..., "arguments": ...} object in plain-text model
    output. Tag-agnostic on purpose -- confirmed live 2026-08-21 that
    deepseek-r1:14b is inconsistent about wrapping this in <tool_call>,
    <tools>, or no tag at all, but the JSON object itself came back
    correctly-schema'd (right field names) in every real test, unlike
    native tool_calls mode which hallucinated wrong field names.

    Returns a LIST, not just the first match -- confirmed live 2026-08-21
    that despite the system prompt saying "one function per response", the
    model sometimes emits several call objects back-to-back in a single
    response (e.g. write two files then run a command). Silently taking
    only the first one drops real, correctly-formed work the model already
    did -- confirmed as a real bug this way (a second file's write_file
    call was dropped, breaking the task) before this was made to scan the
    rest of the string instead of stopping at the first match.

    Returns (calls, cleaned_content) as of 2026-08-29 (root-caused by Fable,
    relayed via github-projects-bf): qwen3-coder intermittently emits a
    well-formed <function=NAME><parameter=KEY>VALUE</parameter></function>
    block with a DANGLING </tool_call> and no matching opener -- confirmed
    live in .../ollama-worker-logs/20260829T165416Z.json. Ollama's native
    parser keys on the opening tag to enter tool-parsing mode, so the whole
    call passes through as plain content and tool_calls comes back empty.
    Worse, the malformed assistant message goes back into context verbatim,
    and the model imitates its own prior formatting on the next turn --
    one slip becomes a dead job. cleaned_content has any successfully-
    salvaged XML call (and its wrapper tags, present or not) stripped, so
    the CALLER can overwrite the stored message and break that lock-in --
    see the call site's own comment for why mutating msg["content"] in
    place is sufficient (messages.append(msg) already holds a reference to
    the same dict, not a copy)."""
    # Pre-pass: normalize Python-style triple-quoted values into real JSON
    # strings. Confirmed live 2026-08-22 (deepseek-r1:32b, clamshell task):
    # the model emitted
    #     {"name": "write_file", "arguments": {"path": "Sources/Clamshell/
    #      ConfirmationBridge.swift", "content": """// swiftlint:disable all
    # -- a correct call, at a correct path, with `"""` where JSON needs `"`.
    # json.loads rejects it, the call was discarded, and the run scored
    # files=0 as though the model had done nothing. This must run BEFORE the
    # brace scan below: `"""` opens-then-closes-then-reopens a string as far
    # as the scanner is concerned, so the span would be found wrong too.
    #
    # Anchored on the closing `"""` being followed by the object's closing
    # braces, so a legitimate `"""` INSIDE the content (a Python docstring)
    # doesn't terminate the match early and truncate the file being written.
    content = re.sub(
        r'(:\s*)"""(.*?)"""(?=\s*\}\s*\})',
        lambda m: m.group(1) + json.dumps(m.group(2)),
        content,
        flags=re.DOTALL,
    )

    calls = []
    # Mistral/devstral [TOOL_CALLS] payloads first -- consumed and blanked out so
    # the brace scan below can't re-find the same objects (see helper).
    content = _extract_tool_calls_token(content, calls)
    pos = 0
    while True:
        # Match either key order. Confirmed empirically that a model emitting
        # {"arguments": {...}, "name": "..."} was dropped entirely by the
        # name-then-arguments-only pattern -- it scores 0 while calling tools
        # correctly, which is the exact failure class this session kept
        # mistaking for model incompetence.
        # Confirmed live 2026-08-28 (llama3.1:8b, NV-Energy model bake-off): a model
        # can emit a correctly-shaped call using "parameters" instead of "arguments"
        # as the payload key (mirroring the tool DEFINITION schema's key name rather
        # than the call schema's) -- the old arguments-only regex never matched at
        # all, so this call was invisible to the fallback and read as "no tool call
        # yet", nudging the model away from what was actually a real, usable call.
        # Third alt added 2026-09-09: the flat {"tool": NAME, ...} dialect (args
        # as sibling keys, not nested) -- devstral emitted this in ```json fences
        # and every call was invisible to the old two-alt pattern.
        m = re.search(
            r'\{.*?"name"\s*:.*?"(?:arguments|parameters)"\s*:'
            r'|\{.*?"(?:arguments|parameters)"\s*:.*?"name"\s*:'
            r'|\{[^{}]*?"(?:tool|tool_name)"\s*:',
            content[pos:], re.DOTALL)
        if not m:
            break
        start = pos + m.start()
        depth = 0
        end = None
        # String-aware brace matching. A naive depth counter also counts
        # braces that appear INSIDE a JSON string literal -- i.e. inside
        # the file content a model is writing -- so any unbalanced brace
        # in generated code (a `}` in a comment, a truncated snippet)
        # ends the candidate at the wrong offset and the whole tool call
        # is silently dropped. Skip over string literals entirely.
        in_str = False
        esc = False
        for i in range(start, len(content)):
            c = content[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break
        candidate = content[start:end + 1]
        try:
            parsed = json.loads(candidate)
        except Exception:
            # Confirmed live 2026-08-21 (qwen2.5-coder:14b, model-bakeoff run):
            # models embedding a shell command inside the "arguments" string
            # sometimes copy shell-escape backslashes (e.g. `\(`, `\)` from a
            # `find ... \( -o ... \)` snippet) verbatim without doubling them,
            # producing a backslash followed by a character JSON doesn't
            # recognize as an escape (strict json.loads raises "Invalid
            # \escape" on this, unlike some lenient parsers). Repair by
            # doubling any backslash not already followed by a valid JSON
            # escape char, then retry once before giving up on this
            # candidate -- same spirit as _fix_literal_escapes above, but
            # for the opposite direction (under- rather than over-escaped).
            #
            # Second repair, confirmed live 2026-08-22 (deepseek-r1:32b,
            # clamshell ConfirmationBridge task): a model can escape its
            # quotes correctly (\"Clamshell\") while emitting REAL newline
            # characters inside the "content" string rather than \n. JSON
            # forbids raw control characters in string literals, so
            # json.loads raises and the tool call is thrown away -- the
            # worker then treats a write_file attempt as a final text
            # answer, stops, and `swift build` passes on an untouched tree
            # (exit=0, files_changed=0), which reads as a clean pass. Try
            # each repair alone and then combined before giving up.
            parsed = None
            for repair in (
                _repair_invalid_json_escapes,
                _escape_raw_control_chars,
                lambda s: _escape_raw_control_chars(_repair_invalid_json_escapes(s)),
            ):
                try:
                    parsed = json.loads(repair(candidate))
                    break
                except Exception:
                    continue
            if parsed is None:
                pos = end + 1
                continue
        coerced = _coerce_tool_call(parsed)
        if coerced is not None:
            calls.append(coerced)
            pos = end + 1
        else:
            # The balanced object parsed but isn't a tool call itself -- it's a
            # WRAPPER around one: {"tool_call": {...}}, {"function": {...}}.
            # Skipping to end+1 would step over the real call nested inside and
            # return nothing. Rescan from just inside this object instead, so
            # the inner call is found on the next pass.
            pos = start + 1

    # qwen3-coder XML salvage (2026-08-29, see this function's own docstring).
    # Matches the function block with an OPTIONAL <tool_call> before and an
    # OPTIONAL </tool_call> after, in one span -- covers properly-wrapped,
    # dangling-closer-only, and bare-with-no-wrapper-at-all uniformly, so the
    # whole thing (wrapper artifacts included) comes out in one removable match
    # rather than needing a second pass to clean up stray tags afterward.
    _valid_tool_names = {t["function"]["name"] for t in TOOLS}
    _xml_call_re = re.compile(
        r'(?:<tool_call>\s*)?<function=([\w_]+)>(.*?)</function>(?:\s*</tool_call>)?',
        re.DOTALL)
    _param_re = re.compile(r'<parameter=([\w_]+)>(.*?)</parameter>', re.DOTALL)
    cleaned_content = content
    for m in _xml_call_re.finditer(content):
        name = m.group(1)
        if name not in _valid_tool_names:
            # Doesn't validate -- almost certainly the model legitimately
            # quoting/discussing the XML syntax itself (e.g. explaining the
            # tool format), not an actual malformed call. Leave it alone
            # entirely: don't add a call, don't touch cleaned_content for
            # this span, so real prose that happens to look like this isn't
            # silently mangled.
            continue
        args = {}
        for pm in _param_re.finditer(m.group(2)):
            key, value = pm.group(1), pm.group(2)
            # Trim exactly ONE newline immediately adjacent to the tags, not
            # all whitespace -- a value can legitimately start or end with
            # meaningful blank lines (e.g. file content), only the formatting
            # newline the model puts right after <parameter=KEY>\n and right
            # before \n</parameter> is an artifact of the tag layout itself.
            if value.startswith("\n"):
                value = value[1:]
            if value.endswith("\n"):
                value = value[:-1]
            if key == "additional":
                # The one integer-typed param across every tool schema
                # (request_more_iterations) -- everything else is a string.
                try:
                    value = int(value.strip())
                except ValueError:
                    pass  # leave as string; the tool impl's own validation catches it
            args[key] = value
        calls.append({"name": name, "arguments": args})
        cleaned_content = cleaned_content.replace(m.group(0), "", 1)
    return calls, cleaned_content.strip()


def _escape_raw_control_chars(s: str) -> str:
    """Escape literal newline/CR/tab characters appearing INSIDE a JSON
    string literal, leaving structural whitespace between tokens alone.
    JSON forbids raw control characters inside strings; some models emit
    them anyway when writing multi-line file content. Tracks string state
    so it never touches the JSON's own formatting."""
    out = []
    in_str = False
    esc = False
    for ch in s:
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == '\\':
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            out.append(ch)
            in_str = False
            continue
        out.append({'\n': '\\n', '\r': '\\r', '\t': '\\t'}.get(ch, ch))
    return ''.join(out)


def log(msg):
    print(msg, flush=True)


def resolve_path(cwd: Path, path: str) -> Path:
    p = (cwd / path).resolve()
    if cwd.resolve() not in p.parents and p != cwd.resolve():
        raise ValueError(f"path escapes working directory: {path}")
    return p


def tool_list_files(cwd: Path, args: dict) -> str:
    """List a directory. Added 2026-08-22 after probing showed BOTH 7B
    models reach for a listing tool that did not exist: qwen2.5-coder:7b
    called read_file({"path": "."}) (reading a directory as a file), and
    deepseek-r1:7b invented a `list_files` call outright and then
    hallucinated the directory contents. Without this, a small model's
    only route to discovery was guessing filenames -- which is exactly
    what produced the fabricated Flask/MERN stacks in the bake-off."""
    p = resolve_path(cwd, args.get("path") or ".")
    if not p.exists():
        return f"ERROR: path not found: {args.get('path')}"
    if not p.is_dir():
        return f"ERROR: not a directory: {args.get('path')} (use read_file for files)"
    skip = {".git", "node_modules", ".next", ".build", "__pycache__", ".venv"}
    entries = []
    try:
        _all = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name))
        if _wl is not None:   # PROTECT_HARNESS: the reference solution is invisible (no-op unless armed)
            _all = [c for c in _all if not _wl.ph_hidden(cwd, c)]
        if len(_all) > _wr.LIST_FILES_MAX_ENTRIES:
            # HUGE DIRECTORY (2026-10-09: a review listed `.` = $HOME, 2149 entries / 82 KB, and
            # every later turn re-read that). Refuse with a short sample and a way forward.
            _sample = [(c.name + "/") if c.is_dir() else c.name for c in _all[:25]]
            return (f"ERROR: {args.get('path') or '.'} has {len(_all)} entries (more than "
                    f"{_wr.LIST_FILES_MAX_ENTRIES}) -- too large to list. Call list_files on a "
                    f"specific subdirectory, or read_file / run_bash grep a path you already know. "
                    f"First entries: {', '.join(_sample)} ...")
        for child in _all:
            if child.name in skip:
                entries.append(f"{child.name}/  (skipped: large/generated)")
            elif child.is_dir():
                entries.append(f"{child.name}/")
            else:
                entries.append(f"{child.name}  ({child.stat().st_size} bytes)")
    except Exception as e:
        return f"ERROR listing directory: {e}"
    if not entries:
        return "(empty directory)"
    return "\n".join(entries)


def tool_read_file(cwd: Path, args: dict, max_chars: int = None,
                   num_ctx: int = None) -> str:
    # Confirmed live 2026-08-28 (llama3.1:8b, request_more_iterations test dispatch): a
    # missing required argument on any of read_file/write_file/edit_file raised a bare
    # KeyError via direct dict indexing, stringified as just the key name (e.g. "'content'")
    # with zero explanation -- same failure class already fixed for web_fetch's "url". Applying
    # the same explicit-check-with-clear-message pattern to all three file tools here.
    if "path" not in args:
        return 'ERROR: read_file requires a "path" argument.'
    if _wl is not None:
        _ph = _wl.ph_file_verdict(cwd, args["path"], "read")      # PROTECT_HARNESS (no-op unless armed)
        if _ph:
            return _ph
    p = resolve_path(cwd, args["path"])
    if not p.exists():
        return f"ERROR: file not found: {args['path']} -- use list_files to see what exists."
    if p.is_dir():
        # Do not dead-end here: a bare "Is a directory" errno was what the
        # models hit before list_files existed, with no hint what to do next.
        return (f"ERROR: {args['path']} is a directory, not a file. "
                f"Listing it instead:\n" + tool_list_files(cwd, {"path": args["path"]}))
    try:
        text = p.read_text()
    except Exception as e:
        return f"ERROR reading file: {e}"
    return _paginate_read(text, args, args["path"], max_chars=max_chars, num_ctx=num_ctx)


def _read_file_cap(max_chars: int | None, num_ctx: int | None) -> int:
    """Whichever budget is SMALLER: the flag/default, or ~1/8 of the window.

    A fixed 16000-char page is ~4K tokens, which is fine at 64K but is a
    quarter of a 16K window. The clamp keeps one read from dominating a small
    dispatch's context, which is the failure this whole change exists to stop.
    """
    cap = max_chars if max_chars is not None else READ_FILE_MAX_CHARS
    if num_ctx:
        cap = min(cap, max(2000, num_ctx * 4 // 8))
    return cap


def _paginate_read(text: str, args: dict, shown_path: str,
                   max_chars: int | None = None, num_ctx: int | None = None) -> str:
    """Return one page of `text`, with a notice saying how to get the next.

    LINES for the range, CHARS for the cap -- deliberately mixed. The model that
    triggered this natively emitted line-based offset/length, so the parameters
    speak lines; the cap protects the context window, which is a size question,
    so it counts chars.

    NEVER REFUSES. A hard "file too large, use offset" dead-ends weaker models
    and burns an iteration to learn something the first page could have told
    them -- and the first page is genuinely useful. Cap and explain instead.

    NO LINE-NUMBER PREFIXES ON BODY LINES. Models paste read_file output
    straight into edit_file's old_string, and a "  312| " prefix would poison
    the exact match -- turning a read improvement into an edit regression. All
    range information lives in the header and footer only.
    """
    def _int(v):
        try:
            return int(str(v).strip())
        except Exception:
            return None

    lines = text.splitlines(keepends=True)
    total_lines, total_bytes = len(lines), len(text)
    cap = _read_file_cap(max_chars, num_ctx)

    off = _int(args.get("offset"))
    ln = _int(args.get("length"))
    start = max(1, off) if off else 1
    if _READ_WINDOW[0] and (ln is None or ln < _READ_WINDOW[0]):
        ln = _READ_WINDOW[0]
    _mw_asked = ln
    if _wl is not None:      # READ_MIN_WINDOW=1: 0 < length < 120 is widened (default OFF = unchanged)
        ln = _wl.min_window_length(ln)
    if total_lines and start > total_lines:
        return (f"ERROR: offset {start} is past the end of {shown_path} -- the file has "
                f"{total_lines} lines. Call read_file with "
                f'{{"path":"{shown_path}","offset":1}} to start over.')
    end_excl = (start - 1 + ln) if (ln and ln > 0) else total_lines
    window = lines[start - 1:end_excl]

    # Trim to WHOLE lines within the char cap: a page cut mid-line is a page a
    # model cannot safely copy into edit_file.
    kept, used, cut_by_cap = [], 0, False
    for line in window:
        if used + len(line) > cap and kept:
            cut_by_cap = True
            break
        kept.append(line); used += len(line)
    body = "".join(kept)
    last = start - 1 + len(kept)
    complete = (start == 1 and last >= total_lines)
    _mw_note = (_wl.min_window_note(_mw_asked, start, last, total_lines)
                if (_wl is not None and ln != _mw_asked) else "")
    if complete:
        return body + (("\n" + _mw_note) if _mw_note else "")   # small file, unchanged behaviour -- no notice noise

    nxt = last + 1
    header = (f"[read_file {shown_path}: lines {start}-{last} of {total_lines} "
              f"({total_bytes} bytes total){', page truncated at ' + str(cap) + ' chars' if cut_by_cap else ''}]\n")
    if nxt > total_lines:
        footer = (f"\n[end of file -- lines {start}-{last} of {total_lines} shown, "
                  f"this is the last page]")
    else:
        # The LITERAL next call, with the real numbers already filled in. A
        # notice that only says "use offset" makes the model do arithmetic it
        # gets wrong; this one can be copied verbatim.
        suggest = max(1, len(kept)) if kept else 300
        footer = (f"\n[{total_lines - last} more line(s) not shown. To continue, call "
                  f'read_file with {{"path":"{shown_path}","offset":{nxt},"length":{suggest}}}]')
    if _mw_note:
        footer += "\n" + _mw_note
    return header + body + footer


def _fix_literal_escapes(content: str) -> str:
    """Some models (confirmed: devstral:24b) emit write_file content with
    literal two-character `\\n`/`\\t` sequences instead of real newline/tab
    bytes -- a generation quirk, not a JSON-parsing bug (Ollama's own
    tool_calls arguments field already decodes cleanly; the model just
    wrote backslash-n as text). Heuristic: only fix this when the content
    has ZERO real newlines but at least one literal `\\n` -- a file that
    already has real newlines mixed with a few genuinely-intended literal
    backslash sequences (e.g. a regex) is left alone, to avoid mangling
    correct output from models that don't have this quirk."""
    if "\n" not in content and "\\n" in content:
        content = (content.replace("\\r\\n", "\n").replace("\\n", "\n")
                          .replace("\\t", "\t").replace('\\"', '"'))
    return content


_SYNTAX_GUARD_ON = [True]    # run_task sets this from the profile (robust.syntax_guard); env WORKER_SYNTAX_GUARD=0 also kills it


def _syntax_guard_reject(rel_path, before, after):
    """Post-edit syntax LINT-DELTA guard (SWE-agent edit linter / Agentless lint delta, 2026-10-09).
    -> an ERROR string when applying `after` over `before` would introduce a syntax error the file
    did not already have (nothing is written -- the edit is REJECTED), else None. Pre-existing
    errors never block; unknown file types, a missing parser, a timeout, or any internal failure
    all mean None (the guard can only ever reject, never break a dispatch)."""
    try:
        if not _SYNTAX_GUARD_ON[0] or not _wr.syntax_guard_enabled():
            return None
        err = _wr.syntax_delta(rel_path, before, after)
        if not err:
            return None
        shown = ""
        m = re.search(r"line (\d+)", err)
        if m:
            ln = int(m.group(1))
            lines = after.splitlines()
            if 1 <= ln <= len(lines):
                shown = f"\nOffending line {ln} of the result: {lines[ln - 1].strip()[:200]}"
        return (f"ERROR: your change to {rel_path} was REJECTED and NOT applied -- it would introduce a "
                f"syntax error that the file did not have before. Parser said: {err}{shown}\n"
                f"The file is unchanged. Fix the new text (balanced brackets/quotes, correct indentation, "
                f"no half-finished statement) and send the edit again.")
    except Exception:
        return None


def tool_write_file(cwd: Path, args: dict) -> str:
    if "path" not in args:
        return 'ERROR: write_file requires a "path" argument.'
    if "content" not in args:
        return 'ERROR: write_file requires a "content" argument.'
    if _wl is not None:
        _ph = _wl.ph_file_verdict(cwd, args["path"], "write")     # PROTECT_HARNESS (no-op unless armed)
        if _ph:
            return _ph
    p = resolve_path(cwd, args["path"])
    content = _fix_literal_escapes(args["content"])
    before = None
    try:
        if p.is_file():
            before = p.read_text()
    except Exception:
        before = None
    rejected = _syntax_guard_reject(args["path"], before, content)
    if rejected:
        return rejected
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _drop_stale_pyc(p)
    return f"OK: wrote {len(content)} bytes to {args['path']}"


def _edit_miss_hints(content: str, old_string: str, new_string: str) -> str:
    """Aider-style failure hints for an edit_file whose old_string was not found: 'this change looks
    already applied' when new_string is already in the file, and a 'did you mean' with the closest
    real lines (with line numbers). Pure, bounded, never raises."""
    try:
        out = []
        if new_string and new_string.strip() and new_string in content:
            n = content[:content.index(new_string)].count("\n") + 1
            out.append(f"NOTE: this change looks ALREADY APPLIED -- your new_string is already present in the "
                       f"file (starting at line {n}). Do not repeat the edit: re-read around that line, then "
                       f"run the verify or call task_complete if the task is done.")
        flines = content.splitlines()
        if 0 < len(flines) <= 40000:
            stripped = [l.strip() for l in flines]
            seen, picks = set(), []
            for ol in [l.strip() for l in old_string.splitlines() if l.strip()][:6]:
                if ol in seen:
                    continue
                seen.add(ol)
                best, best_r = None, 0.0
                for idx, fl in enumerate(stripped):
                    if not fl or abs(len(fl) - len(ol)) > max(8, len(ol)):
                        continue
                    sm = difflib.SequenceMatcher(None, ol, fl)
                    if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
                        continue
                    r = sm.ratio()
                    if r > best_r:
                        best, best_r = idx, r
                if best is not None and best_r >= 0.6:
                    picks.append((best + 1, best_r))
            if picks:
                lines_out = []
                for ln, _r in sorted(set(picks))[:5]:
                    lines_out.append(f"  line {ln}: {flines[ln - 1].rstrip()[:160]}")
                out.append("Did you mean? The closest lines actually in the file (exact text, check "
                           "whitespace/indentation):\n" + "\n".join(lines_out)
                           + f"\nRe-read with read_file offset={max(1, picks[0][0] - 3)} length=20 and copy "
                             f"old_string from there.")
        return "\n".join(out)
    except Exception:
        return ""


def tool_edit_file(cwd: Path, args: dict) -> str:
    """Targeted find/replace, added 2026-08-22 after a real incident: a
    model (qwen3-coder-next) forced to re-emit a whole 762-line file via
    write_file to make one small change generated 35,000+ tokens without
    stopping (confirmed live via Ollama's own generation log -- steady
    ~43 tok/s the entire time, two separate context-window shifts along
    the way) before being killed. Long-verbatim-file reproduction is a
    known LLM failure mode; giving models a small-diff tool instead of
    forcing a full rewrite is the actual fix, not a longer timeout."""
    for required in ("path", "old_string", "new_string"):
        if required not in args:
            return f'ERROR: edit_file requires a "{required}" argument.'
    if _wl is not None:
        _ph = _wl.ph_file_verdict(cwd, args["path"], "write")     # PROTECT_HARNESS (no-op unless armed)
        if _ph:
            return _ph
    p = resolve_path(cwd, args["path"])
    if not p.exists():
        return f"ERROR: file not found: {args['path']}"
    try:
        content = p.read_text()
    except Exception as e:
        return f"ERROR reading file: {e}"
    old_string = _fix_literal_escapes(args["old_string"])
    new_string = _fix_literal_escapes(args["new_string"])
    count = content.count(old_string)
    if count == 0:
        # Whitespace-tolerant fallback (added 2026-08-29): local models -- thinking
        # models especially (nemotron-cascade-2 emitted `let ws  = null` with a
        # double space that isn't in the file) -- routinely mangle INTERIOR
        # whitespace, which fails the exact match. Retry matching the same
        # non-whitespace tokens with each whitespace run treated as flexible
        # (\s+), and apply ONLY if it resolves to EXACTLY ONE span. If it's
        # ambiguous (0 or >1), fall through to the exact-match error -- we never
        # silently edit the wrong location. new_string is spliced literally (no
        # regex substitution), so its contents are never reinterpreted.
        # Split on whitespace runs, escape each non-whitespace token, rejoin with
        # \s+ so any run of whitespace (spaces/tabs/newlines) matches flexibly.
        # (Can't re.escape-then-sub: re.escape also escapes the whitespace chars.)
        _toks = [re.escape(t) for t in re.split(r"\s+", old_string) if t]
        ws_pattern = r"\s+".join(_toks)
        ws_matches = list(re.finditer(ws_pattern, content)) if ws_pattern else []
        if len(ws_matches) == 1:
            m = ws_matches[0]
            new_content = content[:m.start()] + new_string + content[m.end():]
            rejected = _syntax_guard_reject(args["path"], content, new_content)
            if rejected:
                return rejected
            p.write_text(new_content)
            _drop_stale_pyc(p)
            return (f"OK: replaced 1 occurrence in {args['path']} ({len(new_content)} bytes total) "
                    f"[whitespace-tolerant match: your old_string matched except for interior whitespace]")
        _hints = _edit_miss_hints(content, old_string, new_string)
        return (f"ERROR: old_string not found in {args['path']} -- it must match the file's "
                f"current exact content, including whitespace/indentation. To check, re-read "
                f"AROUND the region you are editing: call read_file with an `offset` near it "
                f"(and a small `length`), not a bare re-read -- on a large file a bare read "
                f"returns only the first page, which may not contain your region at all."
                + ("\n" + _hints if _hints else ""))
    if count > 1:
        return (f"ERROR: old_string matches {count} locations in {args['path']} -- it must be "
                f"unique. Include more surrounding context (a line or two before/after) to disambiguate.")
    new_content = content.replace(old_string, new_string, 1)
    rejected = _syntax_guard_reject(args["path"], content, new_content)
    if rejected:
        return rejected
    p.write_text(new_content)
    _drop_stale_pyc(p)
    return f"OK: replaced 1 occurrence in {args['path']} ({len(new_content)} bytes total)"


# Harness-level guard against a dispatch's own tool calls loading a second
# model into the same VRAM pool it's already resident in. Added 2026-08-29
# after a real self-inflicted CUDA OOM: a dispatch model on Unraid (12GB)
# curl'd Ollama's own /api/embed to test the endpoint it was building code
# against, loading nomic-embed-text alongside itself and crashing the next
# request. First fix attempt was prompt-level ("don't call the endpoint
# yourself") -- Fable (peer review) correctly called that out as the same
# mistake the night's own research had just diagnosed: models don't reliably
# obey an instruction not to do something, this needs enforcement at a layer
# that can't be talked out of it, same as the existing repeated-failure hard
# block below. This is deliberately broad (blocks ANY apparent inference call
# via shell, not just ones naming a different model) -- the dispatch harness
# is the only thing that should be making inference calls; a model's own
# tool use has no legitimate reason to hit an LLM serving endpoint directly.
_INFERENCE_ENDPOINT_RE = re.compile(
    r"/api/(generate|chat|embed|pull|create)\b|/v1/(chat/completions|completions|embeddings)\b"
    r"|\bollama\s+(run|pull|create)\b",
    re.IGNORECASE,
)


def _command_risks_self_collision(command: str) -> bool:
    return bool(_INFERENCE_ENDPOINT_RE.search(command or ""))


# Read-only local-inspection commands. A research-kind dispatch whose real sources
# are LOCAL repo files (trace a call path, find why X is gated on Y, locate a perf
# hotspot) verifies through read_file/list_files AND run_bash greps/cats -- so a
# run_bash that reads or searches the tree is genuine local-source verification, the
# same as a read_file. Used only to decide whether a converged research answer with
# zero web_fetch calls actually showed local verification (in which case the
# unverified-provenance warning is a FALSE signal) vs. showed no verification of any
# kind (in which case it is the real signal the warning was built for). Deliberately
# a leading-verb match on the FIRST simple command: a mutation like `rm`/`git commit`
# must not count as a "read" just because a `grep` appears later in the pipeline.
_LOCAL_READ_CMD_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:grep|egrep|fgrep|rg|ag|ack|cat|bat|head|tail|less|more|"
    r"sed|awk|find|fd|ls|tree|wc|nl|cut|sort|uniq|diff|stat|file|"
    r"git\s+(?:grep|log|show|diff|blame|status|ls-files|cat-file))\b",
    re.IGNORECASE,
)


def _command_is_local_read(command: str) -> bool:
    """True when a run_bash command is a read-only inspection of the local tree
    (grep/cat/find/git log/etc.). See _LOCAL_READ_CMD_RE for the rationale."""
    return bool(_LOCAL_READ_CMD_RE.match(command or ""))


def should_stamp_unverified(task_kind: str, web_fetch_succeeded: bool,
                            local_read_count: int, facts_provided: bool) -> bool:
    """Decide whether a converged research answer gets the "claims can't be
    verified" harness stamp. Extracted from run_task's convergence path so the
    property is testable directly rather than through the whole tool loop.

    The stamp is reserved for a research answer that shows NO verification of ANY
    kind: zero successful web_fetch calls AND zero local reads. A research task
    that read/grepped the local repo (local_read_count > 0) performed genuine
    local-source verification and is exempt even with zero web_fetch -- stamping
    it is a false signal (job 8d690b764cec). facts_provided (pass-2 synthesis) is
    always exempt: zero fetches is expected by design there and it has its own
    grounding check. coding-kind tasks are never stamped by this path.
    """
    if task_kind != "research":
        return False
    if facts_provided:
        return False
    return not web_fetch_succeeded and local_read_count == 0


def should_send_zero_fetch_nudge(task_kind: str, web_fetch_succeeded: bool,
                                 local_read_count: int, facts_provided: bool,
                                 already_sent: bool) -> bool:
    """The one-shot "Every web_fetch call in this session has failed" rewrite nudge.

    Same exemption as should_stamp_unverified (2026-10-06): an esc-review /
    local-diagnosis research job reads LOCAL files (read_file/list_files/grep) and
    correctly makes zero web_fetch calls. The nudge told every one of them their
    sources were unread, and the reviews (24123dd86140, 77d808c3984a, a580c8793aa7,
    and ~10 earlier ev-service/chat-fixes ones) spent their final answer arguing
    with the harness instead of diagnosing. Only a research answer with NO
    verification of any kind gets the nudge."""
    if already_sent:
        return False
    return should_stamp_unverified(task_kind, web_fetch_succeeded, local_read_count,
                                   facts_provided)


def _shell_env() -> dict:
    """Environment for every shell the worker spawns on the model's behalf.

    The daemon LaunchAgent's PATH has no ~/bin, so a hint such as
    `run: ollama-dispatch-scaffold --freeze-literals .` (check_literals.py) was
    `command not found` unless the model guessed the absolute path (3f20040dcee9
    burned its last iterations on exactly that). Prepend ~/bin once, here, for
    run_bash AND the verify commands so both see the same tools."""
    env = dict(os.environ)
    home_bin = str(Path.home() / "bin")
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    if home_bin not in parts:
        env["PATH"] = os.pathsep.join([home_bin] + parts)
    # VERIFY-SANDBOX (2026-10-05): the model's shells and its verify are code under
    # test -- ollama-queue.py refuses to enqueue/mutate real jobs from them.
    env["DISPATCH_VERIFY_SANDBOX"] = "1"
    # STALE-BYTECODE HAZARD (2026-10-09, found by the fixed-lane agent): a .pyc is valid while
    # (int mtime, size) of its source match, so an edit that keeps the size within the same
    # second verified against the OLD bytecode. Our shells and verifies never write bytecode,
    # so none can be left behind to go stale; _drop_stale_pyc() removes any pre-existing one
    # for each file the worker writes.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _drop_stale_pyc(p) -> None:
    """Remove the cached bytecode of a .py file the worker just (re)wrote. Never raises."""
    try:
        p = Path(p)
        if p.suffix != ".py":
            return
        pc = p.parent / "__pycache__"
        if pc.is_dir():
            for f in pc.glob(p.stem + ".*.pyc"):
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


class _GroupTimeout(Exception):
    """Raised by _run_shell_group on timeout; carries the partial output."""
    def __init__(self, timeout, stdout, stderr):
        super().__init__(f"command timed out after {timeout}s")
        self.timeout, self.stdout, self.stderr = timeout, stdout or "", stderr or ""


def _run_shell_group(cmd: str, cwd, timeout: int):
    """subprocess.run(shell=True, timeout=) that actually stops the command.

    Plain subprocess.run kills only the direct `sh -c` child on timeout; the
    grandchildren it started (node --test, tsx, npm test, prisma, python3
    refimpl.py ...) survive and keep writing into the worktree after the
    worker has already told the model "timed out" -- and after the end-of-run
    verify has taken its snapshot. ollama-dispatch-auto reproduced and fixed
    the same bug for itself (_run_in_own_process_group); this is the worker's
    copy: the command gets its own session, and on timeout the WHOLE group is
    SIGKILLed. Returns (returncode, stdout, stderr); raises _GroupTimeout with
    whatever partial output was captured so the caller can show the model the
    diagnostics that were printed before the hang instead of nothing."""
    p = subprocess.Popen(cmd, cwd=cwd, shell=True, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env=_shell_env())
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired as te:
        try:
            # start_new_session=True guarantees pgid == p.pid, so the guard is
            # only defence against a future edit -- never kill our own group.
            pgid = os.getpgid(p.pid)
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
            else:
                p.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = te.stdout, te.stderr
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise _GroupTimeout(timeout, out, err) from None


BASH_STREAM_CLIP = 4000


def clip_tool_stream(text, limit=BASH_STREAM_CLIP):
    """PURE. (text, omitted_chars). Keep the last `limit` chars of a run_bash stream, but SAY so:
    job f268674ef763 got only the tail of a 42-row `status` listing with no marker and then
    asserted its 16-row summary was complete. The marker names how much was dropped and how to
    narrow the command (the model cannot otherwise tell a clipped listing from a whole one)."""
    text = text or ""
    if len(text) <= limit:
        return text, 0
    kept = text[-limit:]
    kept = kept[kept.find("\n") + 1:] if "\n" in kept[:200] else kept     # start on a whole line
    om = len(text) - len(kept)
    om_lines = text[:om].count("\n")
    return (f"[OUTPUT TRUNCATED: the first {om} chars (~{om_lines} lines) of {len(text)} were omitted; "
            f"only the LAST {len(kept)} chars are shown below, so any count/summary built from this is "
            f"INCOMPLETE. Re-run narrowed (| grep -c / | grep PATTERN / | cut -c1-120 / | head -n N / "
            f"| sort | uniq -c) before concluding.]\n" + kept), om


def tool_run_bash(cwd: Path, args: dict) -> str:
    # Same explicit-check pattern as the file tools: a missing "command" used to
    # surface as the bare KeyError text "ERROR running command: 'command'".
    if not isinstance(args, dict) or "command" not in args:
        return 'ERROR: run_bash requires a "command" argument (the shell command to run).'
    if _wl is not None:
        _ph = _wl.ph_bash_verdict(cwd, args["command"])           # PROTECT_HARNESS (no-op unless armed)
        if _ph:
            return _ph
    try:
        rc, out, err = _run_shell_group(args["command"], cwd, BASH_TIMEOUT_S)
        if _wl is not None:
            out, err = _wl.ph_scrub(cwd, out), _wl.ph_scrub(cwd, err)
            _wl.ph_note_bash(cwd, args["command"], rc)
        if rc == 0:
            # EDIT_CHECKPOINT: the model ran the task's own verify command itself and it PASSED.
            _edit_checkpoint_on_bash_pass(cwd, args["command"], out)
        return json.dumps({
            "exit_code": rc,
            "stdout": clip_tool_stream(out)[0],
            "stderr": clip_tool_stream(err)[0],
            # The owner 2026-09-20 (Opus root-cause, bonsai rep2 hard-zero): a `cd` in one
            # run_bash call does not persist to the next -- each call is a fresh
            # subprocess at `cwd`. Echoing it back lets a model correct a wrong belief
            # about its location on the VERY NEXT call instead of searching the
            # filesystem for a directory it never left.
            "cwd": str(cwd),
        })
    except _GroupTimeout as t:
        # Keep the ERROR prefix (the loop's failure detection keys on it) but
        # carry the partial output: a slow test run's diagnostics used to vanish.
        tail = ""
        if t.stdout.strip() or t.stderr.strip():
            tail = ("\n--- partial stdout ---\n" + t.stdout[-2000:]
                    + "\n--- partial stderr ---\n" + t.stderr[-2000:])
        return (f"ERROR: command timed out after {BASH_TIMEOUT_S}s "
                f"(the command and everything it spawned were killed){tail}")
    except Exception as e:
        return f"ERROR running command: {e}"


WEB_SEARCH_RETRIES = 2
WEB_SEARCH_RETRY_DELAY_S = 20


_KEYCHAIN_CACHE = {}
# Tavily's free tier is a capped 1000 credits/MONTH (shared across both harnesses), so cap Tavily
# calls per dispatch -- each worker process is exactly one run, so a module-level counter = per-run
# (e2's finding 2026-08-30). Ollama web-search is unmetered + returns full page content, so it goes
# first and does the bulk; Tavily is the quality reserve behind it.
_TAVILY_CALLS = 0
_TAVILY_MAX_PER_RUN = 8
# Per-run tally of which web-search backend actually SERVED a usable result (a fresh worker
# process runs per dispatch, so this starts at 0 each run -- no reset needed). Surfaced in
# dispatch-metrics.jsonl["web_search"] and the queue dashboard's web-search panel. Mutated in
# place (dict item assignment needs no `global`).
_WEB_SEARCH_CALLS = {"ollama": 0, "tavily": 0, "brave": 0, "searxng": 0}


def _keychain_secret(service: str):
    """Fetch a secret from the macOS login Keychain by service name, at call time, cached for the
    process (added 2026-08-30 for Brave/Tavily search keys). Returns None if the item is missing OR
    the keychain is locked -- which is exactly the non-GUI-session case ("User interaction is not
    allowed"): a headless worker after a card-only cold boot won't have the login keychain unlocked,
    so callers must degrade gracefully (fall back to SearXNG) rather than fail. Never logs the value."""
    if service in _KEYCHAIN_CACHE:
        return _KEYCHAIN_CACHE[service]
    val = None
    try:
        acct = os.environ.get("USER") or "user"
        r = subprocess.run(["security", "find-generic-password", "-a", acct, "-s", service, "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            val = r.stdout.strip() or None
        elif "interaction is not allowed" in (r.stderr or "").lower():
            log(f"[worker] web_search: Keychain locked for this session -- can't read {service} "
                f"(non-GUI session); falling back to SearXNG. (Unlock with `security unlock-keychain`.)")
    except Exception:
        val = None
    _KEYCHAIN_CACHE[service] = val
    return val


def _format_search_results(items) -> str:
    """items: iterable of (title, url, snippet). Returns the standard '- title / url / snippet'
    block (top 5) or None if empty, so every backend renders identically to the model."""
    lines = []
    for title, url, snippet in list(items)[:5]:
        if not (url or title):
            continue
        lines.append(f"- {title}\n  {url}\n  {(snippet or '')[:300]}")
    return "\n".join(lines) if lines else None


def _search_tavily(query: str):
    """Tavily -- purpose-built LLM-research search (ranked, answer-shaped). Key: Keychain tavily-api.
    Returns a formatted block, or None to fall through (no key / locked keychain / API error)."""
    key = _keychain_secret("tavily-api")
    if not key:
        return None
    global _TAVILY_CALLS
    if _TAVILY_CALLS >= _TAVILY_MAX_PER_RUN:
        log(f"[worker] web_search: Tavily per-run cap ({_TAVILY_MAX_PER_RUN} credits) reached -- "
            f"skipping Tavily for the rest of this run (falls through to SearXNG).")
        return None
    _TAVILY_CALLS += 1
    try:
        body = json.dumps({"api_key": key, "query": query, "max_results": 5,
                           "search_depth": "basic"}).encode()
        req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return _format_search_results(
            (r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in (data.get("results") or []))
    except Exception as e:
        log(f"[worker] web_search: Tavily failed ({e}) -- falling back")
        return None


def _search_brave(query: str):
    """Brave Search API -- reliable general web. Key: Keychain brave-search-api (X-Subscription-Token).
    Returns a formatted block, or None to fall through."""
    key = _keychain_secret("brave-search-api")
    if not key:
        return None
    try:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": 5})
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "X-Subscription-Token": key})
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return _format_search_results(
            (r.get("title", ""), r.get("url", ""), r.get("description", ""))
            for r in ((data.get("web") or {}).get("results") or []))
    except Exception as e:
        log(f"[worker] web_search: Brave failed ({e}) -- falling back")
        return None


def _search_ollama(query: str):
    """Ollama's HOSTED web-search API (ollama.com/api/web_search) -- free base tier, a real API (not
    scraped consumer SERPs), so it doesn't rate-limit itself into the ground under its own use like
    the self-hosted SearXNG does. NOTE this is a CLOUD search endpoint at ollama.com; it loads no
    model and never touches the Studio/Unraid GPUs, so it is OUTSIDE the no-raw-local-ollama dispatch
    rule (which exists to prevent local VRAM collisions). Called via direct HTTPS, not the ollama MCP.
    Key: Keychain ollama-web-search (Bearer). Returns a formatted block or None to fall through."""
    key = _keychain_secret("ollama-web-search")
    if not key:
        return None
    try:
        body = json.dumps({"query": query, "max_results": 5}).encode()
        req = urllib.request.Request("https://ollama.com/api/web_search", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"}, method="POST")
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return _format_search_results(
            (r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in (data.get("results") or []))
    except Exception as e:
        log(f"[worker] web_search: Ollama web-search failed ({e}) -- falling back")
        return None


def tool_web_search(cwd: Path, args: dict, searxng_host: str) -> str:
    """Tries the free real-API backends first (Tavily, then Ollama web-search), then the free but
    self-degrading SearXNG, then Brave as a PAID last resort (2026-08-30). The two real APIs don't
    rate-limit themselves under their own use the way scraped SearXNG engines do, so a busy research
    run no longer collapses from 3 engines to 1. All keys pulled from Keychain at call time, never
    stored; a locked keychain (non-GUI session) makes that key lookup return None, so the backend is
    simply skipped and the chain degrades gracefully to whatever is reachable. See _search_searxng
    for the original multi-engine + degradation-surfacing behavior."""
    query = args.get("query") if isinstance(args, dict) else None
    if not query:
        if isinstance(args, dict) and args.get("url"):
            return ('ERROR: web_search requires a "query" argument (a search string), not a '
                    '"url" -- that is web_fetch\'s argument. Call web_fetch with that URL instead.')
        return 'ERROR: web_search requires a "query" argument (the search string).'
    # 1. Free real-API backends first, ORDER = COST not quality (e2's finding 2026-08-30): Ollama
    #    web-search FIRST (unmetered + returns full page content, so it does the bulk work), then
    #    Tavily (richer raw_content but a capped 1000 credits/MONTH shared pool, so it's the quality
    #    reserve behind Ollama, and capped per-run inside _search_tavily).
    for _name, _fn in (("Ollama", _search_ollama), ("Tavily", _search_tavily)):
        out = _fn(query)
        if out:
            _WEB_SEARCH_CALLS[_name.lower()] += 1
            log(f"[worker] web_search: served by {_name}")
            return out
    # 2. SearXNG -- free, self-degrading fallback. Returns a "- title/url/snippet" block on real hits,
    #    else a "No results / engines blocked" explanatory message. Distinguish by the result prefix.
    searxng_out = _search_searxng(query, searxng_host)
    if searxng_out and searxng_out.startswith("- "):
        _WEB_SEARCH_CALLS["searxng"] += 1
        log("[worker] web_search: served by SearXNG")
        return searxng_out
    # Brave (PAID -- $5/1k, card bills) is DELIBERATELY NOT in the auto-chain (e2 + the owner's free-
    # services preference, 2026-08-30): a paid fallback that fires unnoticed accumulates a bill
    # quietly, and "last in chain" is not the same as "never fires without a decision". _search_brave
    # stays defined and the key stays in Keychain for a DELIBERATE future choice, but web_search never
    # auto-invokes it. Nothing free had results -> return SearXNG's explanatory message (names the
    # blocked engines, so the model treats empty as an infra gap, not proof the thing doesn't exist).
    return searxng_out or "No results found."


def _capture_decision(capture_target, file_exists, paused_reason, final_text):
    """(should_write, text_to_write, log_tag). dispatch-fixes item 3b.
    Replaces the `capture_final_as and not paused_for_review` guard: a paused run
    still captures its final answer, with a PARTIAL banner so a reader/scorer sees
    it's incomplete rather than the answer being silently lost (was observed live)."""
    if not capture_target:
        return (False, None, "no-target")
    if file_exists:
        return (False, None, "model-wrote-it")   # never clobber the model's own file
    text = (final_text or "").strip()
    if not text:
        return (False, None, "no-final-text")
    if paused_reason:
        banner = (f"<!-- PARTIAL: run paused before completion ({paused_reason}). "
                  f"Captured from the final text answer. -->\n\n")
        return (True, banner + text, "capture=fallback-partial")
    return (True, text, "capture=fallback")


def _annotate_search_coverage(rendered, results, data, engines_queried):
    """Append a coverage warning to a SUCCESSFUL search when engines are down.
    (dispatch-fixes item 3c.) Before this, unresponsive_engines was only read on
    the EMPTY path -- so 4-of-6 engines suspended with 2 still answering gave the
    model a thin result set with no signal that coverage was degraded, the same
    failure class that burned an entire iteration budget with a non-empty set."""
    unresponsive = (data or {}).get("unresponsive_engines") or []
    if not unresponsive:
        return rendered
    names = sorted({(u[0] if isinstance(u, (list, tuple)) and u else str(u)) for u in unresponsive})
    live = max(0, engines_queried - len(names))
    return (rendered + f"\n\n[COVERAGE WARNING: {len(names)} of {engines_queried} search "
            f"engines are currently blocked or unresponsive ({', '.join(names)}). "
            f"These {len(results)} result(s) come from only {live} engine(s). "
            f"Absence of a result here is NOT evidence the thing does not exist -- "
            f"note the gap rather than concluding a negative.]")


def _search_searxng(query: str, searxng_host: str) -> str:
    """Added 2026-08-28 (retry + engine-status surfacing) after a real dispatch
    (the Unraid model-survey task) burned its entire iteration budget hitting
    empty results and never converging -- confirmed live, independent of any
    model, that SearXNG's every enabled general-web engine (brave, duckduckgo,
    google cse, startpage) was simultaneously suspended ("too many requests" /
    CAPTCHA), a known SearXNG operational issue (self-hosted instances get
    bot-detected), not a query problem. Retested ~15-20 min later and it had
    self-cleared with no config change -- so a bounded retry-with-backoff here
    covers the common case (a suspension already near expiry).

    2026-08-28: the retry-with-backoff above was originally paired with a
    claim that the real fix needed a settings.yml change + container restart
    on Unraid (unreachable, no SSH/Docker access) -- that claim was wrong, not
    verified. Checked live via /config: this instance actually has far more
    general-web engines ENABLED than were ever being queried (bing, mojeek,
    qwant, "duckduckgo web" as a distinct engine from plain duckduckgo, yahoo,
    etc, on top of the 4 default ones) -- they just weren't part of the
    *default* engine set a plain /search call (no `engines=` param) hits.
    Confirmed live which of them actually return real results right now
    (`bing` and `duckduckgo web` both did; `qwant` CAPTCHA'd, `yahoo` errored
    -- same class of failure as the original 4, so not worth adding). Now
    passing an explicit `engines=` param naming both the original 4 AND the
    2 newly-confirmed ones, so a simultaneous suspension needs 6 independent
    engines down at once instead of 4 -- a client-side fix, no Unraid access
    needed at all. `mojeek` was tried too and returned zero results for the
    test query without being flagged unresponsive -- inconclusive rather than
    confirmed-working, left out until it's verified on a query with known
    real results.
    When it's STILL empty after retrying, tell the model WHY (which engines are
    down and why) instead of a flat "No results found" -- so it can correctly
    treat that as an infra gap to note, not as proof the thing it searched for
    doesn't exist (the failure mode that burned the whole iteration budget).

    Renamed 2026-08-30 from tool_web_search to _search_searxng: now the SearXNG fallback behind the
    Tavily (free) primary, with Brave (PAID -- $5/1k, card bills; not the free tier it once was) as a
    last resort only when both free paths are empty. Takes `query` directly (no longer the raw args)."""
    url = f"{searxng_host}/search?" + urllib.parse.urlencode({
        "q": query, "format": "json",
        "engines": "brave,duckduckgo,google cse,startpage,bing,duckduckgo web",
    })
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    last_data = None
    for attempt in range(1, WEB_SEARCH_RETRIES + 2):
        try:
            with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            return (f"ERROR: web_search failed ({e}). Is SearXNG deployed and reachable "
                    f"at {searxng_host}?")
        results = (data.get("results") or [])[:5]
        if results:
            lines = []
            for r in results:
                title = r.get("title", "")
                result_url = r.get("url", "")
                snippet = (r.get("content") or "")[:300]
                lines.append(f"- {title}\n  {result_url}\n  {snippet}")
            # 6 = the engines queried on line ~1205; annotate when some were down.
            return _annotate_search_coverage("\n".join(lines), results, data, 6)
        last_data = data
        unresponsive = data.get("unresponsive_engines") or []
        if not unresponsive:
            # Genuinely no results for this query, not an infra issue -- retrying
            # won't help, so stop immediately rather than waste time.
            break
        if attempt <= WEB_SEARCH_RETRIES:
            log(f"[worker] web_search: empty result, {len(unresponsive)} engine(s) "
                f"suspended -- retry {attempt}/{WEB_SEARCH_RETRIES} in {WEB_SEARCH_RETRY_DELAY_S}s")
            time.sleep(WEB_SEARCH_RETRY_DELAY_S)

    unresponsive = (last_data or {}).get("unresponsive_engines") or []
    if unresponsive:
        reasons = ", ".join(f"{name}: {reason}" for name, reason in unresponsive)
        return (f"No results found, and every engine that tried is currently blocked "
                f"({reasons}) -- this looks like a temporary search-infrastructure issue, "
                f"not necessarily an absence of real results for this query. Consider a "
                f"different, more specific query, or note this limitation explicitly in "
                f"your final answer rather than treating the empty result as proof nothing "
                f"exists.")
    return "No results found."


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _extract_aem_json_text(node, out: list, _depth: int = 0) -> None:
    """Walk an AEM content-fragment JSON blob (no fixed schema -- structure
    varies by site config) and collect string values that look like real
    prose rather than IDs/paths/technical keys, so a fetch that lands on a
    JS-rendered page's data endpoint can still return something useful. A
    plain length+space heuristic (>=15 chars, contains a space) is crude but
    effective for the common case: real sentences/labels pass, short
    slugs/UUIDs/booleans-as-strings don't."""
    if _depth > 12:  # guard against a pathological/cyclic structure
        return
    if isinstance(node, str):
        if len(node) >= 15 and " " in node:
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _extract_aem_json_text(v, out, _depth + 1)
    elif isinstance(node, list):
        for v in node:
            _extract_aem_json_text(v, out, _depth + 1)


def _try_aem_model_json(url: str, timeout: int) -> str | None:
    """Best-effort fallback for JS-rendered (typically Adobe AEM) pages:
    the same content is often exposed as fetchable JSON alongside the
    rendered page. Convention varies by site -- try the two most common
    ones. Returns extracted text, or None if neither variant yields
    anything useful (never raises -- this must never turn a real fetch
    failure into a crash, only into the existing plain error message)."""
    base = url.rstrip("/")
    for candidate in (f"{base}.model.json", f"{base}/_jcr_content.model.json"):
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
        except Exception:
            continue  # not this variant -- try the next, or give up quietly
        strings: list = []
        _extract_aem_json_text(data, strings)
        text = "\n".join(dict.fromkeys(strings))  # de-dupe, preserve order
        if len(text) >= 200:
            return f"[NOTE: page was JS-rendered; content extracted from its AEM {candidate.rsplit('/', 1)[-1]} endpoint instead]\n{text}"
    return None


def tool_web_fetch(cwd: Path, args: dict, max_chars: int = None) -> str:
    """Added 2026-08-28: PDF handling. Confirmed live during a real research
    dispatch (NV Energy tariff research, qwen3.5:9b) that fetching a PDF
    (a real NV Energy rate-schedule PDF the model found via web_search --
    exactly the kind of document real research legitimately needs to read)
    fell through to the raw-bytes-as-UTF8 fallback below, since trafilatura
    is an HTML extractor and returns nothing useful for PDF bytes. That
    garbage (raw PDF binary decoded as if it were UTF-8 text -- compressed
    streams, control bytes, the works) got returned as the tool result and
    fed back into the next Ollama /api/chat call, which then failed with a
    hard 500 Internal Server Error, crashing the whole dispatch. Root cause
    confirmed by reading the actual crashed transcript, not guessed. Fix:
    detect PDF content (both by Content-Type header AND by magic bytes,
    since servers sometimes mislabel) before ever reaching the HTML
    fallback, and extract real text via pypdf -- never let raw binary reach
    the raw-decode path, which is what caused the crash."""
    # Confirmed live 2026-08-28 (llama3.1:8b, EV-charging discovery dispatch): a model
    # can call web_fetch with a "query" argument (web_search's schema) instead of "url"
    # -- direct dict indexing raised a bare KeyError, which stringifies to just "'url'"
    # with no explanation of what went wrong or how to fix it, burning a tool-call turn
    # on confusion rather than a correction the model could actually act on.
    url = args.get("url")
    if not url:
        if args.get("query"):
            return ('ERROR: web_fetch requires a "url" argument (a specific page to read), '
                     'not a "query" -- that\'s web_search\'s argument. Use web_search first '
                     'to find a URL, then call web_fetch with that exact URL.')
        return 'ERROR: web_fetch requires a "url" argument (the full URL of the page to fetch).'
    # A real browser UA, not a self-identifying "(ollama-worker)" string --
    # confirmed 2026-08-28 this specific string doesn't explain any actual
    # failure seen so far (a live 404 tested identically with this UA, a
    # real Chrome UA, and no UA at all -- genuinely a dead link, not a
    # block), but plenty of other sites' WAFs do filter on a self-declared
    # bot UA even when this one didn't -- a real browser string removes
    # that whole failure class for future fetches at zero cost/risk.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            html = resp.read()
    except Exception as e:
        return f"ERROR: web_fetch failed: {e}"

    is_pdf = "pdf" in content_type.lower() or html[:5] == b"%PDF-"
    if is_pdf:
        if pdfplumber is None and pypdf is None:
            return ("ERROR: web_fetch got a PDF at this URL, but neither pdfplumber nor pypdf "
                     "is installed -- can't extract its text. Try a different source, or search "
                     "for a non-PDF page covering the same information.")
        # Confirmed live 2026-08-28 (NV-Energy rate PDF, llama3.1:8b verify-fetch dispatch):
        # pypdf's plain extract_text() reads a real two-column page in an order that scrambles
        # a table's labels away from their values -- a "HOW TO CALCULATE YOUR BILL" side-box's
        # text landed BETWEEN "Winter" and its "$0.08658" figure in the linear output, so a
        # model correctly declined to report a value it could no longer associate with its
        # label. pdfplumber's extract_text(layout=True) preserves the page's visual column
        # structure instead, keeping "Winter... $0.08658" on one line -- verified directly
        # against this exact document. Prefer it; fall back to pypdf only if pdfplumber isn't
        # installed or itself fails, so a missing/broken pdfplumber degrades instead of breaking
        # PDF fetches entirely.
        text = ""
        if pdfplumber is not None:
            try:
                with pdfplumber.open(io.BytesIO(html)) as pdf:
                    pages = [p.extract_text(layout=True) or "" for p in pdf.pages]
                text = "\n\n".join(pages).strip()
            except Exception:
                text = ""
        if not text and pypdf is not None:
            try:
                reader = pypdf.PdfReader(io.BytesIO(html))
                pages = [p.extract_text() or "" for p in reader.pages]
                text = "\n\n".join(pages).strip()
            except Exception as e:
                return f"ERROR: web_fetch found a PDF at this URL but failed to parse it: {e}"
        if not text:
            return ("ERROR: web_fetch found a PDF at this URL but could not extract any "
                     "text from it (it may be a scanned image with no text layer). Try a "
                     "different source.")
    else:
        text = None
        if trafilatura is not None:
            text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
        if not text:
            # Added 2026-08-28: this fallback used to dump the FULL raw HTML
            # (up to WEB_FETCH_MAX_CHARS) whenever trafilatura found no
            # extractable main content -- confirmed live during a real
            # research dispatch (NV Energy, qwen3.5:9b) that this is nearly
            # always useless AND expensive: two JS-rendered nvenergy.com
            # pages each dumped a full 8027-char wall of <head>/meta-tag/
            # script-loader boilerplate with zero real content, burning 55%
            # of that run's entire context budget on garbage and directly
            # contributing to it running out of room before writing its
            # actual answer. Fix: strip tags/scripts/styles crudely first
            # (no new dependency -- stdlib re + html.unescape) and check
            # whether there's real substance left. If there is, that's
            # usually a page trafilatura was just too conservative about
            # (still return it, plain-stripped rather than raw markup --
            # much more compact either way). If not, this is almost always
            # a JS-rendered shell with no real static content -- return a
            # short, clear error instead of thousands of wasted chars, so
            # the model can try a different source instead of ingesting soup.
            stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html.decode("utf-8", errors="replace"),
                               flags=re.DOTALL | re.IGNORECASE)
            stripped = re.sub(r"<[^>]+>", " ", stripped)
            stripped = html_module.unescape(stripped)
            stripped = re.sub(r"[ \t]+", " ", stripped)
            stripped = re.sub(r"\n\s*\n+", "\n\n", stripped).strip()
            if len(stripped) >= 200:
                text = f"[NOTE: main-content extraction unavailable, showing tag-stripped page text instead]\n{stripped}"
            else:
                # Added 2026-08-28: confirmed live during a real research
                # dispatch (NV Energy TOU EVRR, llama3.1:8b) that this exact
                # error path fired on three different nvenergy.com URLs, all
                # JS-rendered (Adobe AEM) shells with zero static content --
                # the model then fabricated specific numbers rather than
                # admit it never got real data. AEM sites commonly expose
                # the same content as fetchable JSON alongside the rendered
                # page (the convention varies by site: some serve it at
                # `<path>.model.json`, others need `_jcr_content` inserted
                # before the last path segment) -- try both before giving up.
                aem_text = _try_aem_model_json(url, WEB_TIMEOUT_S)
                if aem_text:
                    text = aem_text
                else:
                    return (f"ERROR: web_fetch could not extract any real content from this page "
                             f"(likely JavaScript-rendered -- the raw HTML has no meaningful static "
                             f"text, only {len(stripped)} chars after stripping markup; also tried "
                             f"this site's AEM .model.json content-fragment endpoints, no luck). "
                             f"Try a different URL for this information -- a PDF version, a cached "
                             f"copy, or a different source entirely.")

    # Confirmed live 2026-08-28 (llama3.1:8b, NV-Energy verify-fetch dispatch): the
    # global 5000-char default was tuned for open-ended multi-fetch research (avoids
    # accumulated-context OOM across 2-3 fetches in one pass), but that same limit
    # silently truncates a single real multi-page document before the actually-needed
    # content -- a real NV Energy rate PDF's page-1 boilerplate alone consumed the
    # whole budget, so the page-2 table with the actual answer never reached the
    # model. It correctly said "not specified" rather than fabricate -- the real bug
    # was truncation, not the model. `max_chars` lets a caller who knows a dispatch is
    # doing few, targeted fetches (not open-ended multi-fetch collection) raise this
    # per-dispatch via --web-fetch-max-chars without weakening the safe default for
    # everyone else.
    limit = max_chars if max_chars is not None else WEB_FETCH_MAX_CHARS
    truncated = len(text) > limit
    text = text[:limit]
    if truncated:
        text += f"\n\n[TRUNCATED at {limit} chars]"
    return text


_TOOL_ARG_NAMES = {
    t["function"]["name"]: set((t["function"].get("parameters") or {}).get("properties") or {})
    for t in TOOLS
}


def _tool_arg_names(name: str) -> set:
    """Argument names a tool actually declares. Derived from TOOLS, never a
    second hand-maintained list -- the whole read_file incident was the schema
    and the behaviour disagreeing, and a duplicated arg list would be the same
    bug in a new place."""
    return _TOOL_ARG_NAMES.get(name, set())


_DIAG_MOD = None


def _diag_mod():
    """Lazy-load dispatch-diagnostics.py (hyphenated filename) from next to this
    file, falling back to ~/bin, so the worker keeps running when the module is
    absent -- the tool then refuses with a visible reason instead of crashing."""
    global _DIAG_MOD
    if _DIAG_MOD is None:
        for cand in (Path(__file__).resolve().parent / "dispatch-diagnostics.py",
                     Path.home() / "bin" / "dispatch-diagnostics.py"):
            if cand.exists():
                spec = importlib.util.spec_from_file_location("dispatch_diagnostics", cand)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _DIAG_MOD = mod
                break
    return _DIAG_MOD


def make_tool_request_diagnostics(verify: str = None):
    """One Budget per run_task: the per-run call cap lives in the closure, so
    a resumed transcript starts a fresh budget (deliberate -- a resume is a
    new review-granted allowance, like extra iterations)."""
    mod = _diag_mod()
    budget = mod.Budget() if mod else None

    def tool(cwd: Path, args: dict) -> str:
        if mod is None:
            return "ERROR: request_diagnostics is unavailable on this host (dispatch-diagnostics.py not found); use run_bash."
        reqs = args.get("requests") if isinstance(args, dict) else None
        if isinstance(reqs, str):
            try:
                reqs = json.loads(reqs)
            except json.JSONDecodeError:
                return "ERROR: `requests` must be a JSON list of request objects, e.g. [{\"kind\":\"git\",\"args\":[\"log\",\"--oneline\",\"-n\",\"10\"]}]"
        if not isinstance(reqs, list) or not reqs:
            return "ERROR: `requests` must be a non-empty list of request objects (kinds: git, file, ls, grep, verify, docker_logs)."
        if _wl is not None:
            _pd = _wl.ph_diag_verdict(cwd, reqs)                  # PROTECT_HARNESS (no-op unless armed)
            if _pd:
                return _pd
        res = mod.run_requests(reqs, cwd, budget=budget, verify=verify)
        return json.dumps(res, ensure_ascii=False)

    return tool


def build_tool_impls(searxng_host: str, web_fetch_max_chars: int = None,
                     read_file_max_chars: int = None, num_ctx: int = None,
                     verify: str = None) -> dict:
    return {
        "request_diagnostics": make_tool_request_diagnostics(verify),
        "list_files": tool_list_files,
        # A lambda, not a bare ref, so the page cap and the window size reach
        # it -- read_file was the last bare function here and that is exactly
        # why it had no way to know how big the context was.
        "read_file": lambda cwd, args: tool_read_file(
            cwd, args, max_chars=read_file_max_chars, num_ctx=num_ctx),
        "write_file": tool_write_file,
        "edit_file": tool_edit_file,
        "run_bash": tool_run_bash,
        "web_search": lambda cwd, args: tool_web_search(cwd, args, searxng_host),
        "web_fetch": lambda cwd, args: tool_web_fetch(cwd, args, max_chars=web_fetch_max_chars),
    }


def _manifest_path(models_root: Path, model: str) -> Path:
    # "qwen3-coder-next:q4_K_M" -> manifests/registry.ollama.ai/library/qwen3-coder-next/q4_K_M
    # "qwen3.6" -> .../qwen3.6/latest
    # "MFDoom/deepseek-r1-tool-calling:14b" -> .../registry.ollama.ai/MFDoom/deepseek-r1-tool-calling/14b
    #
    # Only OFFICIAL models live under "library/"; a namespaced (org/user)
    # model sits directly under the registry root. Hardcoding "library"
    # here silently cost MFDoom/deepseek-r1-tool-calling:14b both of its
    # build-off tasks on 2026-08-22 -- ensure_model_cached raised
    # "not found in SMB source" in 0s and the driver recorded exit=1,
    # files=0, which reads as a model failure rather than a harness bug.
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    root = models_root / "manifests" / "registry.ollama.ai"
    if "/" in name:
        return root / Path(name) / tag
    return root / "library" / name / tag


def _manifest_digests(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text())
    digests = [manifest["config"]["digest"]] + [l["digest"] for l in manifest["layers"]]
    return [d.replace(":", "-") for d in digests]


def log_model_pull(model: str, size_bytes: int, duration_s: float) -> None:
    MODEL_PULL_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(), "model": model,
        "size_gb": round(size_bytes / 1024**3, 2), "duration_s": round(duration_s, 1),
    }
    with MODEL_PULL_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log(f"[worker] logged model pull: {entry}")


def _normalize_model_name(m: str) -> str:
    n, _, t = m.partition(":")
    return f"{n}:{t or 'latest'}"


# ---- shared per-host model inventory (2026-11-06) ----------------------------------
# GET /api/tags on Ollama v0.40.15 runs GetModel() for EVERY pulled model, i.e. it
# parses GGUF metadata off the array disks (~215 GB on the Unraid host): it is NOT a
# cheap "is the host up" probe. It used to be sent once per newly queued job (the
# queue daemon's pre-launch fit pass, via _get_model_size_on_host) and again once per
# dispatch (ensure_model_ready). A host's model list only changes when WE pull or copy
# a model, so it is read once and shared instead:
#   * DISPATCH_HOME/ollama-inventory.json -- {"hosts": {<url>: {"fetched_at": epoch,
#     "models": {"name:tag": size_bytes}}}} -- written atomically (unique temp file +
#     os.replace), shared by the daemon, every worker and the CLI;
#   * an in-process memo of that file, revalidated against its (inode, mtime, size) so
#     another process's write or invalidate is seen on the very next call;
#   * a 60 s in-process failure memo, so a host that is down costs one timeout per
#     minute instead of one per caller. A failed fetch never erases a good list.
# An entry is fresh for OLLAMA_INVENTORY_TTL_S (default 4 h; 0 = never cache, i.e. the
# old read-every-time behaviour). A model that is looked up but absent triggers at most
# one forced re-read per OLLAMA_INVENTORY_MISS_REFRESH_S (default 900 s), which covers a
# model pulled by hand. Our own pulls/copies call invalidate_host_inventory().
def _inv_env_seconds(name: str, default: float) -> float:
    """A non-negative number of seconds from the environment; `default` on anything unusable."""
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)
    return v if v >= 0 else float(default)


OLLAMA_INVENTORY_FILE = DISPATCH_HOME / "ollama-inventory.json"
OLLAMA_INVENTORY_TTL_S = _inv_env_seconds("OLLAMA_INVENTORY_TTL_S", 4 * 3600)
OLLAMA_INVENTORY_MISS_REFRESH_S = _inv_env_seconds("OLLAMA_INVENTORY_MISS_REFRESH_S", 900)
OLLAMA_INVENTORY_FAIL_MEMO_S = 60.0

_INV_LOCK = threading.RLock()
_inv_file_memo = {"sig": None, "hosts": {}}   # parsed ollama-inventory.json + its stat signature
_inv_unsaved = {}                             # host key -> entry the file could not be written for
_inv_fail = {}                                # host key -> (monotonic ts, error text) of the last failed fetch


def _inv_now() -> float:
    """Wall clock: `fetched_at` is shared between processes."""
    return time.time()


def _inv_mono() -> float:
    """Monotonic clock: the in-process failure memo."""
    return time.monotonic()


def _inv_key(host_url: str) -> str:
    return str(host_url or "").strip().rstrip("/")


def _inv_num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _inv_clean_hosts(raw) -> dict:
    """PURE. Validate a parsed ollama-inventory.json -> {host key: {"fetched_at": float,
    "models": {name: size|None}}}. Anything malformed is dropped; never raises."""
    out = {}
    hosts = raw.get("hosts") if isinstance(raw, dict) else None
    if not isinstance(hosts, dict):
        return out
    for key, ent in hosts.items():
        if not isinstance(ent, dict):
            continue
        fetched_at, models = _inv_num(ent.get("fetched_at")), ent.get("models")
        if fetched_at is None or not isinstance(models, dict):
            continue
        out[str(key)] = {"fetched_at": float(fetched_at),
                         "models": {n: _inv_num(s) for n, s in models.items()
                                    if isinstance(n, str) and n}}
    return out


def _inv_load_hosts() -> dict:
    """The validated contents of the inventory file ({} if missing or corrupt), through the
    in-process memo. The memo is trusted only while the file's (inode, mtime, size) are
    unchanged; callers must not mutate the returned dict."""
    try:
        st = os.stat(OLLAMA_INVENTORY_FILE)
    except OSError:
        _inv_file_memo.update(sig=None, hosts={})
        return {}
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    if _inv_file_memo["sig"] == sig:
        return _inv_file_memo["hosts"]
    try:
        hosts = _inv_clean_hosts(json.loads(OLLAMA_INVENTORY_FILE.read_text()))
    except (OSError, ValueError):
        hosts = {}
    _inv_file_memo.update(sig=sig, hosts=hosts)
    return hosts


def _inv_get(key: str):
    """The stored entry for host `key`, or None. An entry that could not be written to the
    file (read-only home, full disk) still serves this process while it is the newer one."""
    ent = _inv_load_hosts().get(key)
    mine = _inv_unsaved.get(key)
    if mine is not None and (ent is None or mine["fetched_at"] > ent["fetched_at"]):
        return mine
    return ent


def _inv_write(hosts: dict) -> bool:
    """Atomically replace the inventory file with `hosts` (unique temp name per process, so
    concurrent writers never share a temp file). False, logged, on an I/O error."""
    tmp = OLLAMA_INVENTORY_FILE.with_name(
        f"{OLLAMA_INVENTORY_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        OLLAMA_INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"hosts": hosts}, indent=1, sort_keys=True) + "\n")
        st = os.stat(tmp)           # os.replace keeps the temp file's inode/mtime/size: the file's signature
        os.replace(tmp, OLLAMA_INVENTORY_FILE)
        # Our own write is the memo's new truth, with the exact signature of what we installed (never a
        # re-stat of the path: another process may already have replaced it again).
        _inv_file_memo.update(sig=(st.st_ino, st.st_mtime_ns, st.st_size), hosts=hosts)
        return True
    except OSError as e:
        log(f"[worker] ollama-inventory: could not write {OLLAMA_INVENTORY_FILE} ({e}); "
            f"keeping the list in memory only")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _inv_fetch(host_url: str, timeout: float) -> dict:
    """ONE GET <host>/api/tags -> {normalized "name:tag": size}. Raises on any failure."""
    req = urllib.request.Request(f"{host_url}/api/tags")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        tags = json.loads(resp.read())
    models = {}
    for m in tags.get("models") or []:
        name = m.get("name")
        if name:
            models[_normalize_model_name(name)] = _inv_num(m.get("size"))
    return models


def _inventory_entry(host_url: str, force: bool = False, timeout: float = 15,
                     ignore_failure_memo: bool = False):
    """The host's inventory entry ({"fetched_at", "models"}), or None when the host could not be
    read. Fresh entry -> returned with NO network. Otherwise exactly one GET /api/tags (held under
    the process lock so threads coalesce), stored for every process. A fetch that failed within the
    last OLLAMA_INVENTORY_FAIL_MEMO_S is not retried (unless ignore_failure_memo); a failure never
    overwrites the stored list."""
    key = _inv_key(host_url)
    with _INV_LOCK:
        ent = _inv_get(key)
        if ent is not None and not force and 0 <= _inv_now() - ent["fetched_at"] < OLLAMA_INVENTORY_TTL_S:
            return ent
        failed = _inv_fail.get(key)
        if failed is not None and not ignore_failure_memo \
                and _inv_mono() - failed[0] < OLLAMA_INVENTORY_FAIL_MEMO_S:
            return None
        t0 = _inv_mono()
        try:
            models = _inv_fetch(key, timeout)
        except Exception as e:
            _inv_fail[key] = (_inv_mono(), str(e) or type(e).__name__)
            log(f"[worker] ollama-inventory: GET {key}/api/tags failed ({e}); "
                f"not retrying for {OLLAMA_INVENTORY_FAIL_MEMO_S:.0f}s")
            return None
        _inv_fail.pop(key, None)
        ent = _inv_put(key, models, _inv_now())
        log(f"[worker] ollama-inventory: read {len(models)} model(s) from {key}/api/tags "
            f"in {_inv_mono() - t0:.1f}s (shared for up to {OLLAMA_INVENTORY_TTL_S:.0f}s)")
        return ent


def _inv_put(key: str, models: dict, fetched_at: float) -> dict:
    """Store host `key`'s list, keeping every other host's entry."""
    ent = {"fetched_at": fetched_at, "models": models}
    hosts = dict(_inv_load_hosts())
    hosts[key] = ent
    if _inv_write(hosts):
        _inv_unsaved.pop(key, None)
    else:
        _inv_unsaved[key] = ent
    return ent


def _inv_last_error(host_url: str) -> str:
    return (_inv_fail.get(_inv_key(host_url)) or (0, "unreachable"))[1]


def host_model_inventory(host_url: str, force: bool = False, *, timeout: float = 15,
                         ignore_failure_memo: bool = False):
    """{normalized "name:tag": size_bytes} for the models pulled on `host_url`, or None if the host
    could not be read (unreachable -- the same meaning the old per-call /api/tags had). Shared
    across processes and cached for OLLAMA_INVENTORY_TTL_S; `force` re-reads now."""
    ent = _inventory_entry(host_url, force=force, timeout=timeout,
                           ignore_failure_memo=ignore_failure_memo)
    return None if ent is None else dict(ent["models"])


def invalidate_host_inventory(host_url: str) -> None:
    """Forget host_url's list (here and in the shared file): call after anything that changes the
    models a host has (a pull, a LAN copy). The next lookup re-reads it. Never raises."""
    key = _inv_key(host_url)
    try:
        with _INV_LOCK:
            _inv_fail.pop(key, None)
            _inv_unsaved.pop(key, None)
            hosts = dict(_inv_load_hosts())
            if hosts.pop(key, None) is not None:
                _inv_write(hosts)
    except Exception as e:
        log(f"[worker] ollama-inventory: invalidate({key}) failed: {e}")


def _get_model_size_on_host(host_url: str, model: str) -> int | None:
    """Return this model's total size in bytes as reported by a host's own
    /api/tags (through the shared inventory: read at most once per
    OLLAMA_INVENTORY_TTL_S, see host_model_inventory), or None if that host is
    unreachable or doesn't have it. A model missing from the list re-reads it,
    but only when the list is older than OLLAMA_INVENTORY_MISS_REFRESH_S."""
    target = _normalize_model_name(model)
    ent = _inventory_entry(host_url)
    if ent is None:
        return None
    if target in ent["models"]:
        return ent["models"][target]
    if _inv_now() - ent["fetched_at"] >= OLLAMA_INVENTORY_MISS_REFRESH_S:
        ent = _inventory_entry(host_url, force=True)
        if ent is not None:
            return ent["models"].get(target)
    return None


def _host_is_free(host_url: str) -> bool:
    """True if this host's Ollama currently has no model loaded -- the same
    proxy for "busy" used manually via ollama_ps/api_ps throughout
    2026-08-28's dispatches. Fails toward "busy" (False) on any error, so a
    host we can't confirm is free is never treated as free by default."""
    try:
        req = urllib.request.Request(f"{host_url}/api/ps")
        with urllib.request.urlopen(req, timeout=10) as resp:
            ps = json.loads(resp.read())
        return len(ps.get("models") or []) == 0
    except Exception:
        return False


def pick_host(model: str) -> str:
    """Choose which of the two known Ollama hosts to dispatch `model` to.

    Priority, the owner's call 2026-08-28: **Studio first if it's currently
    free, Unraid as parallel overflow capacity, Unraid only ever used when
    the model actually fits it.** (Previously preferred Unraid whenever a
    model fit, to keep Studio free for other things -- reversed because
    Studio is the generally stronger/safer host and Unraid's small 12GB
    VRAM makes it spillover-prone even for models nominally "under" the
    fit threshold; see the ensure_model_ready() spillover check for a case
    that already bit this.) The fit constraint is absolute and NOT
    overridden by busy-ness in either direction: a model that doesn't fit
    Unraid goes to Studio regardless of whether Studio is currently busy
    (queues there -- there's no alternative), and a model that fits Unraid
    only gets routed there when Studio is actually busy (dispatching to
    Studio must never be what blocks a task that could otherwise run on
    Unraid in parallel).

    Reads each host's own model list (its /api/tags, shared and cached by
    host_model_inventory rather than re-queried per call) for the model's real
    size rather than assuming; if neither host currently has it pulled, defaults to Studio
    (unified memory is the safer bet for an unknown-size model -- a bad
    guess there degrades to slow, not broken, whereas guessing Unraid for
    an oversized model repeats the exact spillover this function exists to
    avoid)."""
    # Generalised over the user-editable host table (2026-09-26) WITHOUT changing
    # the decision for the owner's studio+unraid pair: "primary" is BIG_HOST_NAME when
    # configured (the big-budget host, preferred and used as the queue-there
    # fallback), every OTHER configured host is overflow capacity, and an overflow
    # host is only ever eligible when the model's real size provably fits its
    # CONFIGURED budget. A host with usable_bytes=None (unmeasured) can never be
    # proven to fit, so it is never picked by auto-routing -- same stance as an
    # unknown model size below.
    hosts = load_ollama_hosts()
    if not hosts:
        return DEFAULT_HOST
    primary = BIG_HOST_NAME if BIG_HOST_NAME in hosts else next(iter(hosts))
    primary_url = hosts[primary]["url"]

    known_size = None
    for spec in hosts.values():
        size = _get_model_size_on_host(spec["url"], model)
        if size:
            known_size = size
            break

    overflow = []
    if known_size is not None:
        for name, spec in hosts.items():
            if name == primary:
                continue
            ub = spec.get("usable_bytes")
            if ub is not None and known_size <= ub:
                overflow.append((name, spec["url"]))

    if known_size is not None and not overflow:
        log(f"[worker] pick_host: {model} ({known_size/1e9:.1f}GB) fits no overflow host's usable "
            f"budget -- {primary} only, regardless of busy-ness.")
        return primary_url

    if _host_is_free(primary_url):
        log(f"[worker] pick_host: {primary} is free -- using it.")
        return primary_url
    if overflow:
        name, url = overflow[0]
        log(f"[worker] pick_host: {primary} busy, {model} ({known_size/1e9:.1f}GB) fits {name} -- "
            f"using {name} as parallel capacity.")
        return url
    log(f"[worker] pick_host: {primary} busy, {model} size unknown (not pulled anywhere yet) -- "
        f"defaulting to {primary} anyway (queues there; safer than an unverified overflow load).")
    return primary_url


def _model_visible_to_local_ollama(model: str) -> bool:
    """Ask the local Ollama server directly whether it already has this
    model, regardless of which directory it's actually stored in. Added
    2026-08-21 after ensure_model_cached blindly copied a model's full
    blobs into LOCAL_MODEL_CACHE even though it was already sitting in
    Ollama's real (default, OLLAMA_MODELS-unset) directory the whole
    time -- wasted ~24GB of a redundant SMB copy before being caught."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=15) as resp:
            tags = json.loads(resp.read())
    except Exception:
        return False
    names = {_normalize_model_name(m.get("name", "")) for m in tags.get("models", []) if m.get("name")}
    return _normalize_model_name(model) in names


def ensure_model_cached(model: str) -> None:
    """Copy `model`'s blobs from the SMB source into LOCAL_MODEL_CACHE
    (OLLAMA_MODELS normally points here) if not already present. Confirmed
    live 2026-08-21: Ollama's own model-load path over SMB hangs
    indefinitely; a plain file copy from the same share does not -- so
    this, not client-side tuning, is the actual fix. No-op if the model is
    already cached locally (checked two ways: already visible to the
    running local Ollama server via /api/tags -- covers the case where
    OLLAMA_MODELS isn't actually pointed at LOCAL_MODEL_CACHE, e.g. a
    model placed directly in Ollama's default directory -- or already
    present in LOCAL_MODEL_CACHE's own manifest)."""
    if _model_visible_to_local_ollama(model):
        log(f"[worker] {model} already visible to local Ollama (/api/tags) -- skipping SMB copy entirely.")
        return
    local_manifest = _manifest_path(LOCAL_MODEL_CACHE, model)
    if local_manifest.exists():
        log(f"[worker] {model} already cached locally, skipping copy.")
        return
    source_manifest = _manifest_path(SMB_MODEL_SOURCE, model)
    if not source_manifest.exists():
        raise RuntimeError(f"model {model} not found in SMB source at {source_manifest}")

    digests = _manifest_digests(source_manifest)
    log(f"[worker] caching {model} locally ({len(digests)} blob(s)) -- this reads the full "
        f"model over SMB once, expect real time for a large model...")
    start = datetime.now(timezone.utc)
    total_size = 0
    (LOCAL_MODEL_CACHE / "blobs").mkdir(parents=True, exist_ok=True)
    for digest in digests:
        src = SMB_MODEL_SOURCE / "blobs" / digest
        dst = LOCAL_MODEL_CACHE / "blobs" / digest
        if not dst.exists():
            shutil.copyfile(src, dst)
        total_size += dst.stat().st_size
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_manifest, local_manifest)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log(f"[worker] cached {model}: {total_size/1024**3:.1f}GB in {elapsed:.0f}s")
    log_model_pull(model, total_size, elapsed)


def evict_model(model: str) -> None:
    """Remove `model`'s blobs from LOCAL_MODEL_CACHE, but only blobs not
    referenced by any OTHER cached model's manifest -- safe even if models
    share layers. Used by --cleanup-after to avoid permanently consuming
    local disk for models only used occasionally."""
    local_manifest = _manifest_path(LOCAL_MODEL_CACHE, model)
    if not local_manifest.exists():
        log(f"[worker] {model} not in local cache, nothing to evict.")
        return
    this_digests = set(_manifest_digests(local_manifest))

    other_digests = set()
    manifests_root = LOCAL_MODEL_CACHE / "manifests"
    for other_manifest in manifests_root.rglob("*"):
        if not other_manifest.is_file() or other_manifest == local_manifest:
            continue
        try:
            other_digests.update(_manifest_digests(other_manifest))
        except Exception:
            continue

    freed = 0
    for digest in this_digests - other_digests:
        blob = LOCAL_MODEL_CACHE / "blobs" / digest
        if blob.exists():
            freed += blob.stat().st_size
            blob.unlink()
    local_manifest.unlink()
    log(f"[worker] evicted {model} from local cache, freed {freed/1024**3:.1f}GB")


def log_dispatch_to_vault(model: str, task: str, converged: bool, verify_passed, log_path: Path,
                          baseline_no_regression: bool = False,
                          dispatch_tag=None, host=None, task_kind=None) -> None:
    """Best-effort append to the vault's dispatch log -- never raises,
    a logging failure must not fail the actual dispatch. The URL and token come
    from _vault_endpoint() (vault_conf.py); the token is never hardcoded here.

    Writes into the UNIFIED ledger (VAULT_DISPATCH_LOG_BASE, monthly
    Agent-Dispatch-Log-YYYY-MM.md, 2026-09-14) shared with Agent-tool dispatches, so each
    line is prefixed with a `[queue]` source tag to keep the two streams apart.
    dispatch_tag is the queue job id (the queue passes --dispatch-tag <id>); host
    and task_kind round out the identifying fields."""
    # Resolved per call (not at import) so the Basic Memory write-shim cutover -- a new
    # ~/.config/vault/env or a rotated token file -- is picked up by the running daemon.
    vault_url, vault_token = _vault_endpoint()
    if not vault_token:
        log("[worker] vault token not set -- skipping vault dispatch log (task itself is unaffected).")
        return
    status = "converged" if converged else "DID NOT CONVERGE"
    # baseline_no_regression: the verify FAILED but every failure pre-existed the dispatch
    # (0 new from the model's diff). Reported distinctly so this never reads as a clean PASS.
    if baseline_no_regression:
        verify_str = "BASELINE-BROKEN (0 new failures; verify was already failing at start)"
    else:
        verify_str = "PASSED" if verify_passed is True else "FAILED" if verify_passed is False else "not run"
    ident = " ".join(filter(None, [
        f"id=`{dispatch_tag}`" if dispatch_tag else "",
        f"host={host}" if host else "",
        f"kind={task_kind}" if task_kind else "",
    ]))
    # One line per dispatch: task text carries raw newlines/markdown headings that
    # otherwise leak into the log as fake sections and bloat the note past the shim's 2 MiB cap.
    _flat = " ".join(str(task).split())
    entry = (
        f"\n- **{datetime.now(timezone.utc).isoformat(timespec='seconds')}** "
        f"`[queue]` `{model}`{(' ' + ident) if ident else ''} -- {status}, verify {verify_str}\n"
        f"  task: {_flat[:200]}{'...' if len(_flat) > 200 else ''}\n"
        f"  transcript: `{log_path}`\n"
    )
    headers = {"Authorization": f"Bearer {vault_token}", "Content-Type": "text/markdown"}
    base_path = vault_dispatch_log_path()
    # Size rollover: the vault shim rejects notes over 2 MiB with 413, so on 413 spill
    # into Agent-Dispatch-Log-YYYY-MM-part2.md, part3, ... instead of dropping the entry.
    for part in range(1, 8):
        path = base_path if part == 1 else base_path[:-3] + f"-part{part}.md"
        url = f"{vault_url}/vault/{urllib.parse.quote(path)}"
        try:
            req = urllib.request.Request(url, data=entry.encode(), method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            log("[worker] dispatch logged to the vault.")
            return
        except urllib.error.HTTPError as he:
            if he.code == 413:
                continue
            if he.code == 404:
                try:
                    header = (f"# Agent Dispatch Log — {datetime.now(timezone.utc):%Y-%m}"
                              f"{'' if part == 1 else f' (part {part})'}\n\n"
                              "Monthly-rotated dispatch log (see the 2026-09-27 vault OOM: keep entries "
                              "small, one file per month; spills to -partN near the 2 MiB cap).\n")
                    req = urllib.request.Request(url, data=(header + entry).encode(), method="PUT", headers=headers)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        resp.read()
                    log("[worker] dispatch log note created.")
                except Exception as e2:
                    log(f"[worker] vault logging failed on note create (non-fatal): {e2}")
                return
            log(f"[worker] vault logging failed (non-fatal): {he}")
            return
        except Exception as e:
            log(f"[worker] vault logging failed (non-fatal): {e}")
            return


CHAT_TIMEOUT_S = 1200


def _stream_idle_limits():
    """(first_chunk_s, idle_s) stall limits for streaming chat reads. Env-overridable
    (WORKER_STREAM_FIRST_S default 600, WORKER_STREAM_IDLE_S default 240); read at call
    time. 0/invalid disables the limit (falls back to the request timeout)."""
    def _g(k, d):
        try:
            return float(os.environ.get(k, d))
        except (TypeError, ValueError):
            return float(d)
    return _g("WORKER_STREAM_FIRST_S", 600), _g("WORKER_STREAM_IDLE_S", 240)


def _arm_stream_stall(resp, seconds):
    """Set the per-recv socket timeout on an open streaming response, so a silent
    connection raises TimeoutError (the family the request timeout raises) instead of
    waiting out CHAT_TIMEOUT_S. Any received bytes restart the clock (per-recv timeout)."""
    if not seconds or seconds <= 0:
        return
    try:
        resp.fp.raw._sock.settimeout(seconds)
    except Exception:
        pass


def _iter_stream_lines(resp):
    """Yield lines of a streaming response with a stall limit: FIRST_S until the first
    line (data OR keepalive/empty delta), IDLE_S between lines after. On a stall, log
    one line, close the connection and re-raise the TimeoutError so the caller's
    existing timeout handling (retry / non-streaming fallback / chat-failure) applies."""
    first_s, idle_s = _stream_idle_limits()
    got = False
    _arm_stream_stall(resp, first_s)
    it = iter(resp)
    while True:
        try:
            line = next(it)
        except StopIteration:
            return
        except TimeoutError:
            log(f"[worker] STREAM STALL: no chunk for {int(idle_s if got else first_s)}s "
                f"(first_chunk={not got}) -- aborting request")
            try:
                resp.close()
            except Exception:
                pass
            raise
        if not got:
            got = True
            _arm_stream_stall(resp, idle_s)
        yield line
WARMUP_TIMEOUT_S = 900  # cold model load (weights off disk into RAM/VRAM) can take minutes, not seconds
CHAT_RETRIES = 2

# --- Darkbloom lane restart (2026-10-01) ---------------------------------------
# A provider restart (watchdog relaunch after a bad auto-update: 0.9.15 failed its
# start 3x, ~6 min down each, 16:23/16:29/16:35) answers in-flight requests with
# HTTP 500 + empty body, then refuses connections. The two CHAT_RETRIES burned in
# seconds and the job paused chat_request_failed. On the Darkbloom lane a 5xx or
# connection error now first WAITS on <host>/health (bounded backoff, up to
# LANE_RESTART_WAIT_S, which covers a ~6 min outage); a retry after the lane was
# seen DOWN and came back is not charged against CHAT_RETRIES (at most
# LANE_RESTART_MAX_CYCLES per request). A 5xx while /health stays OK is a real
# server error: normal retry accounting, after a short settle.
LANE_RESTART_WAIT_S = float(os.environ.get("LANE_RESTART_WAIT_S", "480"))
LANE_RESTART_MAX_CYCLES = int(os.environ.get("LANE_RESTART_MAX_CYCLES", "3"))
LANE_5XX_SETTLE_S = float(os.environ.get("LANE_5XX_SETTLE_S", "10"))
# Darkbloom hosts more models than it has slots (max_model_slots); a request for a
# hosted-but-cold model while every slot holds an in-flight request is REFUSED
# (not queued) with "model slot(s) are active; cannot load '<id>'". That is a
# capacity wait, not a failure: back off (SLOT_BUSY_STEP_S doubling, <=60s) for up
# to SLOT_BUSY_WAIT_S cumulative per request, uncharged. 1800s covers a typical
# 35b author iteration finishing (2026-10-03, cold-load support).
SLOT_BUSY_WAIT_S = float(os.environ.get("SLOT_BUSY_WAIT_S", "1800"))
SLOT_BUSY_STEP_S = float(os.environ.get("SLOT_BUSY_STEP_S", "5"))
_DARKBLOOM_LOCAL_HOSTS = ("http://127.0.0.1:8000", "http://localhost:8000")
# The most recent lane-restart cause, read by the chat_request_failed pause so the
# queue row says WHY (pause_meta.lane_cause).
_LANE_LAST_CAUSE = [None]


def _is_darkbloom_lane(host):
    # PLAN (hardening Wave 2): enforce -> the plan decides with no vendor read; shadow -> legacy decides, a
    # difference is logged; off / no plan -> exactly the legacy answer.
    if _pr is not None:
        _p = _pr.worker_is_lane(host)
        if _p is not None:
            return _p
    h = str(host or "").rstrip("/")
    if h.endswith("/v1"):
        h = h[:-3]
    _legacy = h in _DARKBLOOM_LOCAL_HOSTS or bool(_darkbloom_auth_headers(host))
    return _pr.shadow_is_lane(host, _legacy) if _pr is not None else _legacy


def _lane_health_ok(host, timeout=4):
    h = str(host or "").rstrip("/")
    if h.endswith("/v1"):
        h = h[:-3]
    try:
        with urllib.request.urlopen(h + "/health", timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
            return r.status == 200 and str(body.get("status", "ok")).lower() == "ok"
    except Exception:
        return False


def _lane_restart_wait(host, max_wait=None, probe=None, sleep=time.sleep,
                       clock=time.monotonic):
    """Wait out a Darkbloom restart. Returns (outcome, waited_s, saw_down):
      "up"        -- /health OK on the first probe (lane never looked down)
      "recovered" -- /health was down, then OK again within max_wait
      "timeout"   -- still down after max_wait."""
    max_wait = LANE_RESTART_WAIT_S if max_wait is None else max_wait
    probe = probe or (lambda: _lane_health_ok(host))
    t0 = clock()
    if probe():
        return "up", 0.0, False
    delay = 2.0
    while clock() - t0 < max_wait:
        sleep(min(delay, max(0.0, max_wait - (clock() - t0))))
        delay = min(delay * 2, 30.0)
        if probe():
            return "recovered", clock() - t0, True
    return "timeout", clock() - t0, True


def is_darkbloom_slot_busy(detail):
    """PURE. True when an error body is Darkbloom refusing a (cold) model load
    because every model slot is busy -- strings from the 0.9.17 provider binary:
    "N model slot(s) are active; cannot load '<id>'", "... are occupied and eviction
    is disabled for this load; cannot load '<id>'", "cached model slot(s) are
    active; try again when a request finishes", "Selected model slot became
    unavailable for eviction; retry loading '<id>'", and the HTTP 429 "Provider
    capacity is temporarily unavailable." (cold model, memory held by others). It is
    a capacity wait, not a failure: it frees when an in-flight request finishes."""
    return bool(_SLOT_BUSY_RE.search(str(detail or "")))


_SLOT_BUSY_RE = re.compile(
    r"model slot\(s\) are (?:active|occupied)|cannot load '|"
    r"try again when a request finishes|"
    r"slot became unavailable for eviction|"
    # HTTP 429 when a cold model cannot be placed while other models hold the
    # provider's memory (live 2026-10-03, f77f4c8def05/e8ff2cfcba0f: qwen3.6-35b
    # evicted, Gemma jobs running; provider log "Inference failure: capacity").
    # Same capacity wait, same SLOT_BUSY_WAIT_S bound.
    r"provider capacity is temporarily unavailable", re.I)


_CAPACITY_429_RE = re.compile(r"provider capacity is temporarily unavailable", re.I)
COLD_INFEASIBLE_CAUSE = "darkbloom cold load infeasible"     # queue _lane_failure_decide keys on this prefix


class DarkbloomColdLoadInfeasible(RuntimeError):
    """The model is not resident on Darkbloom and the provider's own load gate says it cannot fit
    (HTTP 429 'Provider capacity is temporarily unavailable'). Waiting does not help -- nothing is
    in flight to finish -- so the worker pauses at once (resumable, lane freed) and the queue holds
    the job with an INFRA WAIT instead of retrying for SLOT_BUSY_WAIT_S holding the lane."""


def _cold_infeasible_reason(model):
    """'needs X GB, have Y GB usable (Z short)' when `model` is not loaded and `darkbloom status`
    says its cold load cannot fit, else None. Never raises (any doubt -> None -> the old wait)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "darkbloom_coldgate", str(Path(__file__).resolve().parent / "darkbloom_coldgate.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        inf = mod.cold_infeasible(model)
        return inf["reason"] if inf else None
    except Exception:
        return None


def _slot_busy_retry(host, detail, state, sleep=time.sleep, max_wait=None, model=None,
                     infeasible=None):
    """Darkbloom lane only. True = the endpoint refused a model load because its
    slots are busy; we waited and the caller should retry WITHOUT charging
    CHAT_RETRIES. Bounded by SLOT_BUSY_WAIT_S of cumulative waiting per request,
    after which normal accounting resumes (False)."""
    if not _is_darkbloom_lane(host) or not is_darkbloom_slot_busy(detail):
        return False
    # 429 "capacity temporarily unavailable" for a model that is NOT resident and that the provider
    # says cannot load (memory): no request is in flight to finish, so waiting up to max_wait is a
    # lane hold for nothing. A genuine busy slot (other messages, or a loadable model) still waits.
    if model and _CAPACITY_429_RE.search(str(detail or "")):
        why = (infeasible or _cold_infeasible_reason)(model)
        if why:
            _LANE_LAST_CAUSE[0] = f"{COLD_INFEASIBLE_CAUSE}: {model} {why}"
            log(f"[worker] DARKBLOOM COLD LOAD INFEASIBLE for {model}: {why} -- not waiting "
                f"(nothing in flight to finish); pausing for the queue's INFRA WAIT, not charged.")
            raise DarkbloomColdLoadInfeasible(_LANE_LAST_CAUSE[0])
    max_wait = SLOT_BUSY_WAIT_S if max_wait is None else max_wait
    waited = float(state.get("slot_wait", 0.0))
    if waited >= max_wait:
        _LANE_LAST_CAUSE[0] = (f"darkbloom slots busy for {waited:.0f}s (cap {max_wait:.0f}s): "
                               f"{str(detail)[:160]}")
        return False
    step = min(SLOT_BUSY_STEP_S * (2 ** int(state.get("slot_tries", 0))), 60.0,
               max(1.0, max_wait - waited))
    state["slot_tries"] = int(state.get("slot_tries", 0)) + 1
    state["slot_wait"] = waited + step
    log(f"[worker] DARKBLOOM SLOTS BUSY (model load refused while other requests hold "
        f"the slots) -- waiting {step:.0f}s and retrying, not charged "
        f"({state['slot_wait']:.0f}/{max_wait:.0f}s): {str(detail)[:160]}")
    sleep(step)
    return True


def _lane_restart_retry(host, code, detail, state, wait=None, sleep=time.sleep, model=None,
                        infeasible=None):
    """Called on a failed attempt. True = retry WITHOUT charging CHAT_RETRIES
    (the lane restarted under us and is back, or a cold model load was refused
    because every slot is busy and we waited). False = normal accounting."""
    if not _is_darkbloom_lane(host):
        return False
    if is_darkbloom_slot_busy(detail):
        # a capacity refusal, not a restart: never falls through to the 5xx path
        return _slot_busy_retry(host, detail, state, sleep=sleep, model=model, infeasible=infeasible)
    if code is not None and int(code) == 401 and _darkbloom_key_rotated_retry(host, state, sleep=sleep):
        return True
    if code is not None and not (500 <= int(code) <= 599):
        return False
    if state.get("cycles", 0) >= LANE_RESTART_MAX_CYCLES:
        return False
    what = f"HTTP {code}" if code is not None else "connection error"
    outcome, waited, _down = (wait or _lane_restart_wait)(host)
    if outcome == "recovered":
        state["cycles"] = state.get("cycles", 0) + 1
        _LANE_LAST_CAUSE[0] = (f"darkbloom lane restarting: {what} ({str(detail)[:120]}); "
                               f"/health down {waited:.0f}s, recovered")
        log(f"[worker] LANE RESTART: {what}; Darkbloom /health was down {waited:.0f}s and is "
            f"back -- retrying (not charged; cycle {state['cycles']}/{LANE_RESTART_MAX_CYCLES}).")
        return True
    if outcome == "timeout":
        _LANE_LAST_CAUSE[0] = (f"darkbloom lane down: {what}; /health not OK after "
                               f"{waited:.0f}s")
        log(f"[worker] LANE DOWN: {what}; Darkbloom /health still not OK after {waited:.0f}s.")
        return False
    _LANE_LAST_CAUSE[0] = f"darkbloom server error with /health OK: {what} ({str(detail)[:120]})"
    if code is not None:
        sleep(LANE_5XX_SETTLE_S)   # a drain often 500s just BEFORE /health drops
    return False


def _is_cuda_oom(error_detail: str) -> bool:
    """Matches Ollama's own CUDA-out-of-memory error body, distinct from a generic
    HTTP/network failure -- see call_ollama/call_ollama_streaming's own recovery
    comment for why this specific signature gets a force-unload-and-retry instead of
    the normal same-state retry (which is provably useless against it, confirmed
    live 2026-08-29: 3/3 identical failures at unchanged settings)."""
    return "CUDA error" in error_detail and "out of memory" in error_detail
# Confirmed live 2026-08-28: an Unraid llama3.1:8b dispatch generated past
# n_gen=123,000 tokens with no stop token, triggered a mid-generation
# context-shift (discarding 12,285 tokens just to keep going), and was still
# running when killed -- a genuine runaway-generation loop, not a slow-but-
# real response (confirmed via the container's own live print_timing log,
# not guessed). Nothing anywhere in this file capped response length before
# this, so a model that fails to emit a stop token can burn the entire
# --chat-timeout budget generating nothing useful. 8192 is generous for a
# real long single-turn output (a full file write, a long tool-call
# payload) while nowhere near what a genuine runaway would need to be
# caught early.
DEFAULT_MAX_TOKENS = 8192   # legacy; NOT used as a default any more (profile max_tokens)
# Same incident, second contributing factor -- confirmed via real research
# (github.com/ollama/ollama/issues/3759; ggml-org/llama.cpp discussion
# #3005), not guessed: this file never set repeat_penalty anywhere, so it
# always ran at llama.cpp's neutral default (1.0 = disabled). Combined with
# --temperature 0 (fully greedy decoding, the common case for every
# dispatch tonight for reproducibility), that's a documented, reproducible
# recipe for a self-reinforcing repetition loop once generation drifts --
# nothing pushes it back out. A small, standard penalty (1.1, the commonly
# recommended value) breaks that without materially changing normal output.
DEFAULT_REPEAT_PENALTY = 1.1   # legacy; NOT used as a default any more (profile repetition_penalty)


# (openai_penalty_fields was removed 2026-10-08: model_profile.build_request_fields now
# emits BOTH repeat_penalty (llama-server) and repetition_penalty (Darkbloom/MLX, which
# reads only the latter -- live loops 2026-10-03/04, output_cap_loop 984db5b6a535 et al.).)


_PROFILE_LOG_SEEN = set()


def _profile_fields(model, role, api_style, temperature=None, num_ctx=None, top_p=None,
                    top_k=None, max_tokens=None, repeat_penalty=None, think=None,
                    sampling_step=0, seed=None, host=None):
    """The request fields for (model, role, api): the model card's profile
    (model_profiles.yaml) with explicit non-None caller values layered on top. The
    ONLY place a request body's sampling / max_tokens / thinking control is decided."""
    if _wl is not None and _wl.nothink_run(role):    # NOTHINK_RUN (default ON since 2026-10-15; NOTHINK_RUN=0 = off): thinking off for the whole author run
        think = False
    # REASONING_PARSER=qwen3 (Worker-flags lever, DEFAULT OFF): ask Darkbloom's local endpoint to split Qwen thinking into
    # reasoning_content. Only for an openai-style request to the Darkbloom lane (`host` given); the Unraid/Ollama paths and
    # every non-Darkbloom OpenAI host never get the field. model_profile also requires the profile to declare it.
    _rparser = (_wl.reasoning_parser_request() if (_wl is not None and api_style == "openai" and host
                                                   and _is_darkbloom_lane(host)) else None)
    if _pr is not None:
        try:
            _LAST_OV[0] = tuple(_pr.override_names(
                temperature=temperature, top_p=top_p, top_k=top_k, max_tokens=max_tokens,
                repetition_penalty=repeat_penalty, think=think, seed=seed,
                presence_penalty=(_wl.presence_penalty_override(role) if _wl is not None else None)))
        except Exception:
            _LAST_OV[0] = ()
    _fields = _mp.build_request_fields(
        model, role or "author", "openai" if api_style == "openai" else "ollama",
        overrides={"temperature": temperature, "num_ctx": num_ctx, "top_p": top_p,
                   "top_k": top_k, "max_tokens": max_tokens,
                   "repetition_penalty": repeat_penalty,
                   "presence_penalty": (_wl.presence_penalty_override(role) if _wl is not None else None)},
        think=think, sampling_step=sampling_step, seed=seed, reasoning_parser=_rparser)
    # MODEL_PROFILE_LOG=1 (default OFF): one line per request naming the profile + the effective
    # params that go on the wire (so a transcript shows what each turn was actually sent).
    # 2026-10-10: also ON by default for openai-style (Darkbloom) requests, deduped per distinct
    # (role, fields) so each job log carries the exact wire params once (A/B temps proof). Log-only.
    _mpl = os.environ.get("MODEL_PROFILE_LOG")
    _sig = (model, role or "author", api_style, json.dumps(_fields, sort_keys=True, default=str))
    if _mpl == "1" or (_mpl != "0" and api_style == "openai" and _sig not in _PROFILE_LOG_SEEN):
        _PROFILE_LOG_SEEN.add(_sig)
        try:
            _pp = _mp.get_profile(model, role or "author")
            log(f"[worker] request profile={_pp['profile_key']}:{_pp['mode']} role={role or 'author'} "
                f"api={api_style} sent={json.dumps(_fields, sort_keys=True, default=str)}")
        except Exception as _e:
            log(f"[worker] request profile log failed (ignored): {_e!r}")
    return _fields


def _strip_inline_think(msg):
    """OpenAI-path: with thinking ON some Darkbloom builds put the chain of thought
    INLINE in `content` as a leading <think>...</think>. Move it to `thinking` so it
    never reaches tool parsing / the next turn's history. No-op otherwise."""
    c = msg.get("content")
    if isinstance(c, str) and "</think>" in c:
        m = re.match(r"^\s*(?:<think>)?(.*?)</think>\s*", c, re.S)
        if m:
            msg.setdefault("thinking", m.group(1).strip())
            msg["content"] = c[m.end():]
    return msg

# Structured, one-line-per-dispatch token-usage log -- added 2026-08-28 after
# a real dispatch tonight hit a hard context-exhaustion failure (llama-server:
# "request (68352 tokens) exceeds the available context size (32768 tokens)")
# with no record anywhere of how close to the ceiling past dispatches had
# come. Deliberately JSONL, not prose in the vault dispatch log -- the point
# is to eventually query "what's the actual right --num-ctx for a task this
# size" across many dispatches, which means every entry needs the same fixed
# fields, not a paragraph a human has to re-parse each time.
DISPATCH_METRICS_PATH = LOG_DIR / "dispatch-metrics.jsonl"

# Mutated in place by run_task as the dispatch progresses (not returned,
# since main()'s crash handler needs to see whatever was accumulated even
# when run_task never reaches a normal return -- see write_dispatch_metrics).
_dispatch_metrics: dict = {}


def write_dispatch_metrics(metrics: dict) -> None:
    """Append one JSONL line for this dispatch's token usage, whether it
    converged, failed verify, or crashed outright. Never raises -- a
    metrics-logging failure must not fail (or mask the real error of) the
    actual dispatch. Called both from run_task's normal completion path and
    from main()'s crash handler, so a context-exhaustion crash -- the exact
    failure mode this log exists to eventually let the owner threshold against --
    still gets a real entry instead of silently vanishing."""
    if _pr is not None:
        _pr.summary(metrics)      # request count for the daemon's reap-time GAP check (never raises)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(DISPATCH_METRICS_PATH, "a") as f:
            f.write(json.dumps(metrics) + "\n")
    except Exception as e:
        log(f"[worker] dispatch metrics logging failed (non-fatal): {e}")


# --- Darkbloom (local OpenAI endpoint) auth -------------------------------------
# Darkbloom's `--local-endpoint` is API-key gated. The key lives in
# ~/.darkbloom/local.json (rewritten on every provider restart, so it is read at
# CALL time, never cached or copied into a config/env/argv/log). Returned only
# when `host` is that endpoint, so no other OpenAI-style host (e.g. the old
# llama-server bypass) ever receives it.
# TOCTOU (2026-10-13, queue->worker consistency harness): Darkbloom rewrites local.json on
# every provider restart and the file is momentarily EMPTY. A request built in that window
# carried no Bearer key -> HTTP 401 -> the preflight/chat failed for a host that was fine
# a second later. For the KNOWN Darkbloom endpoint only (the fixed local port, or
# DARKBLOOM_BASE_URL) a missing key is re-read for up to DARKBLOOM_KEY_REREAD_S seconds
# (default 8; 0 = the old single read). No other host ever waits.
def _darkbloom_key_reread_s():
    try:
        return max(0.0, float(os.environ.get("DARKBLOOM_KEY_REREAD_S", "8")))
    except ValueError:
        return 8.0


def _darkbloom_known_host(host):
    h = str(host or "").rstrip("/")
    if h.endswith("/v1"):
        h = h[:-3]
    env = (os.environ.get("DARKBLOOM_BASE_URL") or "").rstrip("/")
    if env.endswith("/v1"):
        env = env[:-3]
    return bool(h) and (h in _DARKBLOOM_LOCAL_HOSTS or (bool(env) and h == env))


# 401 from a key rotated MID-FLIGHT (2026-10-13 review item 6). The request goes out with key K1, the
# provider restarts and rewrites local.json with K2, and the in-flight request (or the next one, built
# before the file is rewritten) is answered 401. That 401 used to take the normal CHAT_RETRIES path,
# whose retries fire back-to-back with whatever key is on disk -- often still K1 -- and burn the
# attempts (preflight / max_attempts=1 callers had none at all). One BOUNDED, UNCHARGED retry per call
# after waiting (<= DARKBLOOM_KEY_ROTATE_WAIT_S, default 8) for local.json's key to differ from the key
# we last SENT. Unchanged key after the wait = a genuinely bad key: normal accounting, no loop.
# Kill switch: DARKBLOOM_401_RETRY=0 (the old behaviour exactly).
_DB_KEY_SENT = {}


def _darkbloom_401_retry_enabled():
    return os.environ.get("DARKBLOOM_401_RETRY", "1").strip() != "0"


def _darkbloom_key_rotate_wait_s():
    try:
        return max(0.0, float(os.environ.get("DARKBLOOM_KEY_ROTATE_WAIT_S", "8")))
    except ValueError:
        return 8.0


def _darkbloom_key_rotated_retry(host, state, sleep=time.sleep, clock=time.monotonic):
    """True = retry now, uncharged (the key on disk changed since the one we sent). At most one per
    `state` (one call). Never raises."""
    if _pr is not None:
        # ENFORCE: lane_plan.refresh_key decides (event-based expiry raises PlanExpired -> resumable pause with
        # a fresh plan on relaunch). Shadow/off: the legacy logic below decides and shadow logs a disagreement.
        try:
            _pv = _pr.worker_rotated_retry(host, state, enabled=_darkbloom_401_retry_enabled())
        except _pr.PlanExpired as _pe:
            _LANE_LAST_CAUSE[0] = str(_pe)
            log(f"[worker] {_pe}")
            raise
        if _pv is not None:
            if _pv:
                try:
                    _w = _pr.current()
                    log(f"[worker] PLAN KEY ROTATED: new key fp {_w.fp} (one uncharged retry; plan {_w.plan.get("plan_id")})")
                except Exception:    # noqa: BLE001
                    log("[worker] PLAN KEY ROTATED (one uncharged retry)")
            return _pv
        return _pr.shadow_rotated_retry(host, _darkbloom_key_rotated_retry_legacy(host, state, sleep=sleep, clock=clock))
    return _darkbloom_key_rotated_retry_legacy(host, state, sleep=sleep, clock=clock)


def _darkbloom_key_rotated_retry_legacy(host, state, sleep=time.sleep, clock=time.monotonic):
    try:
        if not _darkbloom_401_retry_enabled() or state.get("k401", 0) >= 1:
            return False
        h = _norm_openai_host(host)
        sent = _DB_KEY_SENT.get(h)
        if not sent:
            return False                      # we never sent a Darkbloom key: nothing rotated
        state["k401"] = state.get("k401", 0) + 1
        deadline = clock() + _darkbloom_key_rotate_wait_s()
        while True:
            now_key = _darkbloom_local_record().get("api_key")
            if now_key and now_key != sent:
                log(f"[worker] KEY ROTATED mid-flight: HTTP 401 with the key sent, local.json now holds a "
                    f"different one -- retrying once with the new key (not charged).")
                return True
            if clock() >= deadline:
                break
            sleep(0.5)
        log(f"[worker] HTTP 401 and local.json's key is unchanged after "
            f"{_darkbloom_key_rotate_wait_s():.0f}s -- a bad key, not a rotation; normal retry accounting.")
    except Exception:
        pass
    return False


def _darkbloom_auth_headers(host, sleep=time.sleep):
    if _pr is not None:
        _pa = _pr.worker_auth(host)          # ENFORCE: the plan's key, no local.json read; shadow/off: None
        if _pa is not None:
            return _pa
        return _pr.shadow_auth(host, _darkbloom_auth_headers_legacy(host, sleep=sleep))
    return _darkbloom_auth_headers_legacy(host, sleep=sleep)


def _darkbloom_auth_headers_legacy(host, sleep=time.sleep):
    try:
        h = str(host).rstrip("/")
        if h.endswith("/v1"):
            h = h[:-3]
        deadline = time.monotonic() + (_darkbloom_key_reread_s() if _darkbloom_known_host(host) else 0.0)
        while True:
            rec = _darkbloom_local_record()
            base = _darkbloom_base_from_record(rec)
            key = rec.get("api_key")
            if key and base and h == base:
                _DB_KEY_SENT[h] = key
                return {"Authorization": "Bearer " + key}
            if key or time.monotonic() >= deadline:    # a key for ANOTHER endpoint: nothing to wait for
                break
            log(f"[worker] {h}: ~/.darkbloom/local.json has no api_key yet (provider restarting?) -- re-reading")
            sleep(0.5)
    except Exception:
        pass
    return {}


# --- Opt-in key file for any OTHER OpenAI-style endpoint (2026-10-05) -------------
# A bake-off arm against a key-gated server that is not Darkbloom (first use: the
# Strata server on claude-sandbox, reached through an ssh tunnel) passes
# --api-key-file PATH. The key is read from that file at CALL time, never stored
# in argv/env/logs, and is sent ONLY to the --host it was given with -- a
# different host (or the Darkbloom lane, which keeps its own key) never gets it.
# Unset (every queue-launched job) => no header, byte-identical to before.
_OPENAI_KEY_FILE = {"path": None, "host": None}


def _norm_openai_host(host):
    h = str(host or "").rstrip("/")
    return h[:-3] if h.endswith("/v1") else h


def set_openai_key_file(path, host):
    _OPENAI_KEY_FILE["path"] = path or None
    _OPENAI_KEY_FILE["host"] = _norm_openai_host(host) if path else None


def _openai_auth_headers(host):
    """Auth headers for an OpenAI-style request to `host`: Darkbloom's own key when
    `host` is the Darkbloom endpoint, else the --api-key-file key when `host` is the
    host that file was bound to, else none. Never raises."""
    d = _darkbloom_auth_headers(host)
    if d:
        return d
    p, bound = _OPENAI_KEY_FILE["path"], _OPENAI_KEY_FILE["host"]
    if not p or not bound or _norm_openai_host(host) != bound:
        return {}
    try:
        with open(p) as fh:
            key = fh.read().strip()
    except OSError:
        return {}
    return {"Authorization": "Bearer " + key} if key else {}


def _darkbloom_served_models():
    """Lower-cased ids Darkbloom serves (provider.toml enabled/preload_models +
    ~/.darkbloom/loaded-models.json); empty set when unreadable. Never raises.
    PLAN: enforce -> the served set embedded in the plan file; shadow -> legacy + a logged difference."""
    if _pr is not None:
        _ps = _pr.worker_served()
        if _ps is not None:
            return _ps
        return _pr.shadow_served(_darkbloom_served_models_legacy())
    return _darkbloom_served_models_legacy()


def _darkbloom_served_models_legacy():
    import json as _j, os as _o, re as _r
    out = set()
    try:
        txt = open(_o.path.expanduser("~/.config/darkbloom/provider.toml")).read()
        for m in _r.finditer(r"(?m)^\s*(?:enabled_models|preload_models)\s*=\s*\[([^\]]*)\]", txt):
            out |= {x.lower() for x in _r.findall(r"['\"]([^'\"]+)['\"]", m.group(1))}
    except Exception:
        pass
    try:
        out |= {str(x).lower() for x in
                (_j.load(open(_o.path.expanduser("~/.darkbloom/loaded-models.json")))
                 .get("models") or []) if x}
    except Exception:
        pass
    return out


def _is_darkbloom_only_model(model, served=None):
    """A bare id (no Ollama ':tag') Darkbloom serves -- it exists on no Ollama host."""
    m = str(model or "").strip()
    if not m or ":" in m:
        return False
    return m.lower() in (_darkbloom_served_models() if served is None else served)


def _num(v):
    """PURE. v as a finite float >= 0, else None (bool/str/None/garbage all -> None)."""
    try:
        if isinstance(v, bool) or v is None:
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) and f >= 0 else None
    except Exception:
        return None


def usage_from_ollama(raw) -> dict:
    """PURE. Native Ollama counters -> the normalized usage dict. Same three keys as before;
    `eval_duration_s` (generation-only seconds) is a NEW optional key, present only when the
    server reported a positive eval_duration. Never raises."""
    raw = raw if isinstance(raw, dict) else {}
    pt = raw.get("prompt_eval_count") or 0
    ct = raw.get("eval_count") or 0
    usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    ed = _num(raw.get("eval_duration"))
    if ed:
        usage["eval_duration_s"] = ed / 1e9
    return usage


def usage_accumulate(metrics, usage, call_elapsed) -> None:
    """Fold ONE model call's usage into the per-job sums in `metrics` (dispatch-metrics). Only
    values the backend actually reported are summed; a missing/garbage field is skipped, never
    an exception, never a status change (callers also wrap it)."""
    try:
        u = usage if isinstance(usage, dict) else {}
        pt, ct = _num(u.get("prompt_tokens")), _num(u.get("completion_tokens"))
        if pt:
            metrics["sum_prompt_tokens"] = (metrics.get("sum_prompt_tokens") or 0) + int(pt)
        el = _num(call_elapsed)
        if ct and el:
            metrics["sum_call_s"] = round((metrics.get("sum_call_s") or 0) + el, 3)
            metrics["sum_call_completion_tokens"] = (metrics.get("sum_call_completion_tokens") or 0) + int(ct)
        ed = _num(u.get("eval_duration_s"))
        if ct and ed:
            metrics["sum_eval_s"] = round((metrics.get("sum_eval_s") or 0) + ed, 3)
            metrics["sum_eval_completion_tokens"] = (metrics.get("sum_eval_completion_tokens") or 0) + int(ct)
    except Exception:
        pass


def usage_finalize(metrics) -> str:
    """Derive tok_s_overall (completion tokens / wall seconds in model calls, prefill included)
    and decode_tok_s (completion tokens / server-reported eval seconds, Ollama only) from the
    accumulated sums; absent stays absent. -> a one-line summary ('' if nothing was reported)."""
    try:
        cs, ct = metrics.get("sum_call_s"), metrics.get("sum_call_completion_tokens")
        if cs and ct:
            metrics["tok_s_overall"] = round(ct / cs, 1)
        es, et = metrics.get("sum_eval_s"), metrics.get("sum_eval_completion_tokens")
        if es and et:
            metrics["decode_tok_s"] = round(et / es, 1)
        pt, tot = metrics.get("sum_prompt_tokens"), metrics.get("sum_completion_tokens")
        if pt is None and not metrics.get("tok_s_overall"):
            return ""
        return ("[worker] USAGE SUMMARY: prompt_tokens=%s completion_tokens=%s tok_s=%s decode_tok_s=%s calls=%s"
                % (pt if pt is not None else "n/a", tot if tot is not None else "n/a",
                   metrics.get("tok_s_overall", "n/a"), metrics.get("decode_tok_s", "n/a"),
                   metrics.get("calls", "n/a")))
    except Exception:
        return ""


def call_ollama(host: str, model: str, messages: list, temperature: float, num_ctx: int,
                 timeout: int = CHAT_TIMEOUT_S, tools: bool = True,
                 top_p: float = None, top_k: int = None, api_style: str = "ollama",
                 max_tokens: int = None,
                 repeat_penalty: float = None, think=None,
                 preserve_reasoning: bool = False, role: str = "author",
                 tool_choice: str = None, max_attempts: int = None,
                 sampling_step: int = 0, seed: int = None) -> dict:
    """api_style="openai" targets llama-server (or any OpenAI-compatible
    /v1/chat/completions endpoint) instead of Ollama's native /api/chat.
    Added 2026-08-22: confirmed live that Ollama's own chat-template
    validation has a real upstream bug ("no user query found in messages",
    github.com/ollama/ollama/issues/17778) that crashes qwen3.8/devstral
    even on trivial requests -- llama-server renders the GGUF's own embedded
    chat template directly and doesn't run Ollama's custom Go renderer code
    at all, so it doesn't hit this bug. Returns a response already
    normalized to Ollama's shape ({"message": {...}}) so callers don't need
    to know which backend actually served the request."""
    # Profile-driven (model_profiles.yaml): None args => the model card's values for `role`.
    _f = _profile_fields(model, role, api_style, temperature, num_ctx, top_p, top_k,
                         max_tokens, repeat_penalty, think,
                         sampling_step=sampling_step, seed=seed, host=host)
    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)

    if api_style == "openai":
        payload = {"model": model, "messages": messages, "stream": False}
        payload.update(_f)
        if tools:
            payload["tools"] = TOOLS
            if tool_choice:       # 422-recovery ladder only (see _recover_422)
                payload["tool_choice"] = tool_choice
        url = f"{host}/v1/chat/completions"
    else:
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": _f["options"]}
        # think (added 2026-08-30, e2's finding): a top-level Ollama key, native /api/chat only.
        # None => omit (model's default / no toggle). False => disable reasoning so a hybrid model
        # (qwen3.5:9b etc.) doesn't spend its whole token budget on the `thinking` field and return
        # empty content / no tool call. True => force it on. Always-thinking models (nemotron-a3b)
        # reject an explicit value with an HTTP error; there is NO auto-retry that drops think --
        # use --think auto (the default) for those models.
        if "think" in _f:
            payload["think"] = _f["think"]
        if tools:
            payload["tools"] = TOOLS
        url = f"{host}/api/chat"

    last_err = None
    last_err_detail = None
    _lane_state = {}
    attempt = 0
    _budget = (CHAT_RETRIES + 1) if max_attempts is None else max(1, int(max_attempts))
    while attempt < _budget:
        attempt += 1
        # Built per attempt: Darkbloom rotates its key on every provider restart,
        # so a request built once would re-send a stale key on every retry.
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **(_openai_auth_headers(host) if api_style == "openai" else {})},
            method="POST",
        )
        _t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read())
                _conf_rec(host, url[len(host):], api_style, model, payload, req, role, 200, _t0, raw=raw, step=sampling_step)
                if api_style == "openai":
                    # Normalize {"choices": [{"message": {...}}]} to Ollama's
                    # {"message": {...}} shape so the rest of run_task's loop
                    # doesn't need to know which backend answered. llama-server's
                    # /v1/chat/completions already reports usage in the standard
                    # OpenAI {prompt_tokens, completion_tokens, total_tokens}
                    # shape -- pass it straight through.
                    _msg = raw["choices"][0]["message"]
                    # BONSAI / thinking models on llama-server: the model's text
                    # arrives in `reasoning_content`, NOT `content` (confirmed
                    # live against Ternary-Bonsai-2-27B -- see
                    # Claude/Projects/bonsai-bakeoff.md). Every consumer below
                    # reads `content` and `thinking`, so without this the turn
                    # looks empty and --capture-final-as / harvest_reasoning
                    # have nothing to work with.
                    #
                    # OPT-IN, and deliberately so: gated behind --preserve-reasoning
                    # rather than applied to every openai response, so the existing
                    # qwen3.8 llama-server bypass keeps byte-identical behaviour (in
                    # particular it never starts feeding the reasoning-freeze
                    # detector a `thinking` field it has never seen before).
                    #
                    # The content fallback is NARROW on purpose: reasoning is only
                    # promoted to `content` when the model produced no content AND
                    # no tool call, i.e. the turn would otherwise be empty. A turn
                    # that did call a tool keeps content empty, so raw chain-of-
                    # thought is never mistaken for a deliberate answer.
                    _fin = raw["choices"][0].get("finish_reason")
                    _rp_on = "reasoning_parser" in payload
                    if preserve_reasoning:
                        _rc = _msg.get("reasoning_content")
                        if _rc:
                            _msg.setdefault("thinking", _rc)
                            # REASONING_PARSER on: reasoning cut at the cap is a cutoff, not an answer -- it is NOT
                            # promoted into content (that would make it prose); run_task's think-cap recovery owns it.
                            if (not (_msg.get("content") or "").strip() and not _msg.get("tool_calls")
                                    and not (_rp_on and str(_fin or "").lower() == "length")):
                                _msg["content"] = _rc
                    _strip_inline_think(_msg)
                    _ret = {"message": _msg, "usage": raw.get("usage") or {}}
                    if _rp_on:     # extra keys ONLY with the lever on: lever off == the old return value, byte for byte
                        _ret.update({"done_reason": _fin, "_reasoning_parser": True,
                                     "_thinking_on": (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is not False})
                    return _ret
                # Ollama's native /api/chat reports token counts under
                # different keys (prompt_eval_count/eval_count, no
                # total_tokens at all) -- normalize to the same
                # {prompt_tokens, completion_tokens, total_tokens} shape as
                # the openai branch above so run_task's usage tracking
                # doesn't need to know which backend answered either.
                raw["usage"] = usage_from_ollama(raw)
                return raw
        except urllib.error.HTTPError as e:
            # The response BODY (the server's actual error message) was never
            # being read here -- every crash tonight (2026-08-28) had to be
            # reconstructed from raw ollama.log/llama-server stdout after the
            # fact instead of just being visible in the raised error, because
            # str(e) on an HTTPError is just the status line ("HTTP Error
            # 500: Internal Server Error"), not the JSON body a server like
            # llama-server actually sends (e.g. {"error":{"message":"tools
            # param requires --jinja flag", ...}}). Read it once, defensively
            # (the body can itself be unreadable/already consumed).
            try:
                body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                body = "<could not read response body>"
            # Bug fixed 2026-08-29: this used to overwrite last_err with a
            # plain f-string (including the body) instead of the exception
            # object -- `raise ... from last_err` then crashed with
            # "exception causes must derive from BaseException" once retries
            # were exhausted, MASKING the real underlying error (a genuine
            # CUDA OOM, in the case that surfaced this) behind an unrelated
            # TypeError. Keep last_err as the real exception for `from`;
            # carry the body separately for display only.
            last_err = e
            last_err_detail = f"{e} -- body: {body}"
            _conf_rec(host, url[len(host):], api_style, model, payload, req, role, e.code, _t0, step=sampling_step)
            if api_style == "openai" and _lane_restart_retry(host, e.code, last_err_detail, _lane_state, model=model):
                _budget += 1      # the lane restarted under us and is back: not charged
                continue
            if attempt < _budget:
                log(f"[worker] {'llama-server' if api_style == 'openai' else 'Ollama'} request failed ({last_err_detail}), retry {attempt}/{CHAT_RETRIES}...")
                if e.code in (429, 502, 503, 504):
                    # Darkbloom's slots are shared with fleet traffic: at the cap it can
                    # answer 429/503. Back off instead of burning the retries instantly.
                    time.sleep(min(30, 5 * attempt))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            last_err_detail = str(e)
            _conf_rec(host, url[len(host):], api_style, model, payload, req, role, None, _t0, step=sampling_step)
            if (api_style == "openai" and not isinstance(e, TimeoutError)
                    and _lane_restart_retry(host, None, last_err_detail, _lane_state)):
                _budget += 1
                continue
            if attempt < _budget:
                log(f"[worker] {'llama-server' if api_style == 'openai' else 'Ollama'} request failed ({e}), retry {attempt}/{CHAT_RETRIES}...")
    if api_style == "ollama" and last_err_detail and _is_cuda_oom(last_err_detail):
        # Same recovery as call_ollama_streaming's own version of this block -- see
        # its comment for the full reasoning. Gated to native Ollama only: llama-server
        # (api_style="openai") has no /api/generate keep_alive concept to force-unload
        # through, and is a single-model-per-process server anyway, so this specific
        # recovery doesn't apply there.
        log(f"[worker] CUDA OOM detected after normal retries -- force-unloading "
            f"{model} and retrying once more (a fresh load can defragment the CUDA "
            f"pool; a same-state retry cannot, which is why the retries above failed "
            f"identically).")
        set_keep_alive(host, model, "0")
        time.sleep(3)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read())
                raw["usage"] = usage_from_ollama(raw)
                return raw
        except Exception as e:
            log(f"[worker] CUDA OOM recovery retry also failed: {e}")
    raise RuntimeError(f"Chat request failed after retries: {last_err_detail}") from last_err


# ---------------------------------------------------------------------------
# 422 "Inference generation failed" recovery (2026-10-09). Darkbloom 0.9.19 (MTP +
# tool-call generation) answers HTTP 422 invalid_request_error "Inference generation
# failed" (stream: finish_reason "error") DETERMINISTICALLY for some contexts when tools
# are present: the model emits </think> and the server dies at the first tool-call token.
# Identical-body retries can never help. Proven to flip to 200: tool_choice "none" for
# that turn, no tools for that turn, or a shortened prior tool result. After the normal
# retries are exhausted run a bounded ladder, one attempt per step, on a COPY of the
# messages (the real transcript is never altered).
# ---------------------------------------------------------------------------
RECOVERY_422_MAX_ATTEMPTS = 6        # per run; a truly dead server still pauses for review
RECOVERY_422_TRUNC_LINES = 40
RECOVERY_422_NUDGE = ("Continue. Respond with your next tool call now, as text in exactly this format "
                      "(no tool-calling API is available this turn):\n"
                      "<tool_call>\n<function=TOOL_NAME>\n<parameter=PARAM_NAME>\nVALUE\n"
                      "</parameter>\n</function>\n</tool_call>")


def _is_422_gen_failed(err) -> bool:
    s = str(err)
    return ("422" in s) and ("Inference generation failed" in s or "Response generation failed" in s)


def _cut_text(text: str):
    """(shorter text, changed). >40 lines -> first 40 + '[truncated N lines]'; else a very
    long few-line blob -> first 4000 chars + '[truncated N chars]'."""
    lines = text.split("\n")
    if len(lines) > RECOVERY_422_TRUNC_LINES:
        return ("\n".join(lines[:RECOVERY_422_TRUNC_LINES])
                + f"\n[truncated {len(lines) - RECOVERY_422_TRUNC_LINES} lines]"), True
    if len(text) > 4000:
        return text[:4000] + f"\n[truncated {len(text) - 4000} chars]", True
    return text, False


def _truncate_last_tool_result(messages: list):
    """Copy of `messages` whose LAST tool-role message is shortened (first
    RECOVERY_422_TRUNC_LINES lines + a '[truncated N lines]' marker). The worker's tool
    results are usually JSON ({"exit_code":..,"stdout":"a\\nb.."}), i.e. ONE physical line, so
    long string fields inside a JSON object are cut individually and re-dumped. None when
    there is nothing to cut."""
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if m.get("role") != "tool":
            continue
        raw = str(m.get("content") or "")
        new, changed = raw, False
        try:
            obj = json.loads(raw)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if isinstance(v, str):
                    nv, ch = _cut_text(v)
                    if ch:
                        obj[k], changed = nv, True
            if changed:
                new = json.dumps(obj)
        if not changed:
            new, changed = _cut_text(raw)
        if not changed:
            return None
        out = list(messages)
        out[idx] = {**m, "content": new}
        return out
    return None


def _recover_422(call, messages: list, metrics: dict, iteration=None):
    """Run the ladder; `call(msgs, **kw)` is a single-attempt non-streaming request. Returns
    the normalized response of the first step that worked, else None (caller pauses)."""
    def _useful(r):
        m = (r or {}).get("message") or {}
        return bool((m.get("content") or "").strip() or m.get("tool_calls"))
    steps = [
        (1, "tool_choice=none + nudge", lambda: (
            [*messages, {"role": "user", "content": RECOVERY_422_NUDGE}], {"tool_choice": "none"})),
        (2, f"last tool result truncated to {RECOVERY_422_TRUNC_LINES} lines", lambda: (
            _truncate_last_tool_result(messages), {})),
    ]
    rec = metrics.setdefault("recovery_422", {"attempts": 0, "ok": 0, "events": []})
    for n, label, build in steps:
        if rec["attempts"] >= RECOVERY_422_MAX_ATTEMPTS:
            log(f"[worker] 422 recovery: cap of {RECOVERY_422_MAX_ATTEMPTS} attempts reached -- not trying step {n}.")
            return None
        msgs, kw = build()
        if msgs is None:
            log(f"[worker] 422 recovery: step {n} ({label}) skipped -- nothing to truncate.")
            continue
        rec["attempts"] += 1
        try:
            r = call(msgs, **kw)
            ok = _useful(r)
            why = "" if ok else "empty reply"
        except Exception as e:
            r, ok, why = None, False, str(e)[:160]
        rec["events"].append({"iteration": iteration, "step": n, "ok": ok})
        if ok:
            rec["ok"] += 1
        log(f"[worker] 422 recovery: step {n} ({label}) {'ok' if ok else 'failed'}"
            + (f" -- {why}" if why else "") + f" [attempt {rec['attempts']}/{RECOVERY_422_MAX_ATTEMPTS}]")
        if ok:
            r["_recovery_422"] = n
            return r
    return None

# ---------------------------------------------------------------------------
# Opt-in live streaming log (--live-log). Ported from qwen-dispatch.sh's
# embedded Python: the checkpoint-extraction regexes, the margin=20 trick,
# the sentence-boundary-aware snippet(), the None -> 'thinking' -> 'writing'
# phase state machine, and the DIM/YELLOW/CYAN/GREEN/RED ANSI scheme.
# Differences from qwen-dispatch.sh (which is one-shot and tool-less):
#   * every line is prefixed with [dispatch-tag] so several concurrent
#     dispatches sharing one log file stay separable under `tail -f`
#   * tool-call / tool-result lines (magenta) -- qwen-dispatch.sh has no
#     tool loop at all
#   * per-tag rate file (~/qwen-rate-<tag>.txt) instead of ~/qwen-rate.txt
# When --live-log is not passed, none of this runs: call_ollama() is
# untouched and the non-streaming path is exactly what it was before.
# ---------------------------------------------------------------------------

LIVE_DIM = '\033[2m'
LIVE_YELLOW = '\033[1;33m'
LIVE_CYAN = '\033[1;36m'
LIVE_GREEN = '\033[1;32m'
LIVE_RED = '\033[1;31m'
LIVE_MAGENTA = '\033[1;35m'
LIVE_RESET = '\033[0m'

_LIVE_BOLD_RE = re.compile(r'\*\*([^*]{4,80})\*\*')
_LIVE_NUMBERED_RE = re.compile(r'(?:^|\n)\s*(?:\d+[.):]|Step \d+)\s*([A-Z][^\n.]{4,80})', re.MULTILINE)
_LIVE_CHECKLIST_RE = re.compile(r'(?:^|\n)\s*-\s*([A-Z][a-zA-Z ]{2,30}):\s*[^\n]{0,60}?(✓|✗|\bMet\b|\bmatch(?:es)?\b)', re.MULTILINE)


def _live_snippet(text, n=100):
    """Ported from qwen-dispatch.sh: collapse whitespace, then cut at the
    start of the last complete sentence within the tail window so the
    heartbeat reads as a real thought instead of a mid-word fragment."""
    text = ' '.join(text.split())
    if len(text) <= n:
        return text
    tail = text[-n:]
    for sep in ('. ', '? ', '! '):
        idx = tail.rfind(sep)
        if idx != -1 and idx < len(tail) - 15:  # don't cut right at the end
            return tail[idx + len(sep):]
    return tail


def _live_find_new_checkpoints(text, seen, margin=20):
    """Ported from qwen-dispatch.sh: pull out bold headers / numbered-step /
    checklist markers as they appear, so the live view shows real structure
    instead of an arbitrary rolling text window. margin: only accept a match
    that ends at least this many chars before the end of text -- otherwise,
    on a streaming buffer, a still-growing partial line matches a
    slightly-longer version of itself on every token and spams one line per
    token."""
    found = []
    limit = len(text) - margin
    for pattern, build in (
        (_LIVE_BOLD_RE, lambda m: m.group(1).strip()),
        (_LIVE_NUMBERED_RE, lambda m: m.group(1).strip()),
        (_LIVE_CHECKLIST_RE, lambda m: f'{m.group(1).strip()}: {m.group(2)}'),
    ):
        for m in pattern.finditer(text):
            if m.end() > limit:
                continue  # too close to the live edge, may still be growing
            c = build(m)
            if c not in seen:
                seen.add(c)
                found.append(c)
    return found


class LiveLog:
    """Appends tagged, colored status lines to a file a human can `tail -f`
    (the qwen.example.com ttyd terminal already does exactly this for
    qwen-dispatch.sh's log). Every line is prefixed with [tag] because
    multiple concurrent dispatches commonly share one --live-log file. The
    log path is whatever the caller passed -- this file stays host-agnostic
    about where the dashboard lives."""

    def __init__(self, path, tag, model, host):
        self.path = Path(path)
        self.tag = tag
        self.model = model
        self.host = host
        # Per-tag rate file (qwen-dispatch.sh writes ~/qwen-rate.txt for its
        # tmux status bar; with concurrent tagged dispatches sharing a log,
        # one rate file per tag is the direct generalization).
        self.rate_path = Path.home() / f"qwen-rate-{tag}.txt"
        # Line-buffered so `tail -f` sees every line the moment it's written.
        self._fh = open(self.path, "a", buffering=1)
        self.write_rate(f"{model}: dispatch starting")
        self.dispatch_header()

    def _ts(self):
        return time.strftime("%H:%M:%S")

    def emit(self, line, color):
        self._fh.write(f"{LIVE_DIM}[{self._ts()}]{LIVE_RESET} [{self.tag}] {color}{line}{LIVE_RESET}\n")
        self._fh.flush()

    def write_rate(self, text):
        try:
            self.rate_path.write_text(text)
        except Exception:
            pass  # rate file is cosmetic; never fail a dispatch over it

    def dispatch_header(self):
        self.emit("═" * 60, LIVE_CYAN)
        self.emit(f"▶ NEW DISPATCH {self._ts()} — {self.model} @ {self.host}", LIVE_CYAN)
        self.emit("═" * 60, LIVE_CYAN)

    def iteration(self, n, total):
        self.emit(f"── iteration {n}/{total} ──", LIVE_DIM)

    def tool_call(self, name, args):
        parts = []
        for k, v in (args or {}).items():
            s = str(v).replace("\n", " ")
            if len(s) > 60:
                s = s[:57] + "..."
            parts.append(f"{k}='{s}'" if isinstance(v, str) else f"{k}={v!r}")
        self.emit(f"-> calling {name}({', '.join(parts)})", LIVE_MAGENTA)

    def tool_result(self, name, result):
        s = str(result)
        n_lines = s.count("\n") + 1
        # Byte/line count only -- tool results can be huge file reads, and
        # dumping them here would defeat the point of a glanceable live view.
        self.emit(f"<- {name} returned {len(s)} bytes ({n_lines} lines)", LIVE_MAGENTA)

    def result_box(self, text, converged, iterations, status=None):
        # status overrides the converged/not binary for the third real outcome: PAUSED.
        # A paused run is not a failure and must not be painted as one -- after worker
        # batch #7 every legitimately-paused arm (the verify cannot answer the question,
        # a human is needed) reaches here, and reading "NO CONVERGENCE" on a run that
        # stopped deliberately at 5/20 misrepresents both the outcome and the budget.
        if status == "paused":
            color, title = LIVE_YELLOW, "PAUSED FOR REVIEW"
        else:
            color = LIVE_GREEN if converged else LIVE_RED
            title = "RESULT" if converged else "NO CONVERGENCE"
        if len(text) > 4000:
            text = text[:4000] + "\n... [truncated]"
        self.emit(f"┌─ {title} " + "─" * (53 + (6 - len(title))), color)
        for l in text.split("\n"):
            self.emit(f"│ {l}", color)
        self.emit("└" + "─" * 63, color)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


class ChatAbortedForPause(Exception):
    """Raised by call_ollama_streaming when an external SIGTERM pause arrives
    MID-GENERATION (see _sigterm_pause_handler). Bug #1 (2026-09-18): the worker
    only honored SIGTERM BETWEEN iterations, so an in-flight /api/chat generation
    blocked the pause for up to --chat-timeout (a runaway reasoning generation held
    the Studio lane 12+ min and deadlocked the queue behind a regate that could not
    get the lane). The streaming read loop now polls _sigterm_pause_requested each
    chunk and, when set, closes the connection (Ollama cancels the generation on
    client disconnect, freeing the GPU) and raises this so run_task drops straight
    to its resumable-pause save path -- the lane frees in seconds, not minutes, and
    the transcript through the last completed iteration is intact. The blocking
    (non-streaming) call_ollama path is not interruptible this way; the queue
    daemon's bounded SIGTERM->SIGKILL escalation is the backstop for that path."""


# Runaway-reasoning budget (worker_robust.ReasoningBudget; set per run by run_task from the
# profile's robust.reasoning_budget_chars, 0 = off). One-element list so the streaming
# functions read the live value without a signature change.
_REASONING_BUDGET = [0]


class ChatAbortedForReasoningRunaway(Exception):
    """A single turn spent _REASONING_BUDGET[0] chars thinking with no content and no tool call
    (output_cap_loop root cause with 32768 max_tokens + thinking on: the model never leaves the
    reasoning block). Unlike ChatAbortedForReasoningLoop this is NOT a repetition fixed point,
    just unbounded deliberation, so the retry turns thinking OFF (profile non-thinking mode)."""


# No-tool-call PROSE budget for coding turns, in CHARS (0 = off). Armed per run by run_task
# from the profile's robust.prose_budget_tokens (x4 chars/token, the file-wide estimate) for
# coding tasks only. One-element list for the same reason as _REASONING_BUDGET.
_PROSE_BUDGET = [0]
PROSE_GUARD_STRIDE = 1500        # re-check the streamed text every this many new content chars
PROSE_LINE_REPEAT_MIN_LEN = 20   # a line shorter than this ("}", "  ],") may repeat legitimately
PROSE_LINE_REPEAT_MAX = 25       # same >=20-char line this many times in one tool-call-free turn
PROSE_BLOCK_CHARS = 500          # the last 500 chars occurring ...
PROSE_BLOCK_REPEATS = 3          # ... this many times in the turn = a repeated block


class ChatAbortedForProseRunaway(Exception):
    """A coding turn streamed more than the no-tool-call prose budget (or degenerated into a
    repeated line/block) without starting a tool call (smoke job 6cf6fb3afca4, 2026-10-08: four
    turns of ~115K chars / ~400s each, no tool call, until prose_loop fired after ~28 min).
    Carries the partial turn so run_task can treat it as a cut-off turn (existing nudge +
    prose_loop accounting) after persisting the raw text for diagnosis."""
    def __init__(self, reason, content, thinking="", streamed=True):
        super().__init__(reason)
        self.reason, self.content, self.thinking, self.streamed = reason, content, thinking, streamed


class ChatAbortedForReasoningLoop(Exception):
    """Raised by call_ollama_streaming when a SINGLE generation is stuck
    re-deriving the same reasoning inside one turn (the owner 2026-09-20: three
    separate live jobs -- bece829d8005, d08795db97ee, 0971afaccfa9 -- each
    burned 30min-6h temp=0 oscillating between a handful of near-identical
    thinking fragments, never emitting content/tool_calls, until num_predict
    ran out or a human noticed and killed it by hand.

    The existing frozen-reasoning detector (REASONING_FREEZE_*, below) is
    structurally blind to this: it compares COMPLETED turns' thinking blocks,
    and a stuck generation never completes. This one watches the SAME turn's
    think_buf as it streams and aborts the in-flight request the moment the
    tail repeats something already seen earlier in that same buffer --
    mirroring ChatAbortedForPause's resp.close()+raise pattern so the GPU
    frees immediately and run_task drops into its resumable-pause path."""


def _with_thinking(exc, thinking):
    """Attach the aborted turn's streamed reasoning to the exception so run_task can persist it
    (an aborted turn's reasoning was previously discarded, leaving no way to tell a true loop
    from a false positive after the fact)."""
    exc.thinking = thinking
    return exc


def call_ollama_streaming(host: str, model: str, messages: list, temperature: float, num_ctx: int,
                          timeout: int = CHAT_TIMEOUT_S, tools: bool = True,
                          top_p: float = None, top_k: int = None, live: "LiveLog" = None,
                          max_tokens: int = None,
                          repeat_penalty: float = None, think=None,
                          role: str = "author", sampling_step: int = 0, seed: int = None) -> dict:
    """Streaming counterpart of call_ollama() for the native Ollama
    /api/chat path ONLY (api_style="ollama", native tools). Used only when
    --live-log is active: sends the same request with stream: true, parses
    the newline-delimited streamed JSON exactly like qwen-dispatch.sh does
    (msg.get('thinking', ''), msg.get('content', ''), d.get('done')), and
    emits live status lines to the LiveLog as it goes.

    Returns the same normalized shape call_ollama() returns --
    {"message": {...}, "usage": {prompt_tokens, completion_tokens,
    total_tokens}} -- so run_task()'s loop logic downstream doesn't change.
    The --manual-tools / api_style="openai" paths are deliberately NOT
    covered here (out of scope for this pass); run_task falls back to the
    blocking call_ollama for those."""
    _f = _profile_fields(model, role, "ollama", temperature, num_ctx, top_p, top_k,
                         max_tokens, repeat_penalty, think,
                         sampling_step=sampling_step, seed=seed)
    payload = {"model": model, "messages": messages, "stream": True, "options": _f["options"]}
    if "think" in _f:  # native /api/chat top-level key (see call_ollama)
        payload["think"] = _f["think"]
    if tools:
        payload["tools"] = TOOLS
    url = f"{host}/api/chat"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Retry only the connection attempt (same policy as call_ollama). Once
    # the stream is open and tokens are flowing, a mid-stream failure is
    # raised as-is -- retrying would duplicate a partially-consumed
    # generation, which is worse than surfacing the error.
    resp = None
    last_err = None
    last_err_detail = None
    for attempt in range(1, CHAT_RETRIES + 2):
        _t0 = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            _conf_rec(host, "/api/chat", "ollama", model, payload, req, role, 200, _t0, step=sampling_step)
            break
        except urllib.error.HTTPError as e:
            # Same body-reading fix call_ollama() got on 2026-08-29 (see its
            # HTTPError handler for the full story): str(e) is just the status
            # line, not the JSON body a server actually sends.
            try:
                body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                body = "<could not read response body>"
            _conf_rec(host, "/api/chat", "ollama", model, payload, req, role, e.code, _t0, step=sampling_step)
            last_err = e
            last_err_detail = f"{e} -- body: {body}"
            if attempt <= CHAT_RETRIES:
                log(f"[worker] Ollama streaming request failed ({last_err_detail}), retry {attempt}/{CHAT_RETRIES}...")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            last_err_detail = str(e)
            _conf_rec(host, "/api/chat", "ollama", model, payload, req, role, None, _t0, step=sampling_step)
            if attempt <= CHAT_RETRIES:
                log(f"[worker] Ollama streaming request failed ({e}), retry {attempt}/{CHAT_RETRIES}...")
    if resp is None and last_err_detail and _is_cuda_oom(last_err_detail):
        # Fable diagnosis, 2026-08-29 (confirmed live on Unraid: qwen3.5:9b succeeded
        # on iteration 1, failed identically on iteration 2's generation call, 3/3
        # attempts, same settings, same already-resident model): a normal retry can't
        # fix this because it's deterministic -- llama.cpp's CUDA compute buffers scale
        # with actual prompt size, not just num_ctx, and a later agentic iteration
        # carries the whole prior turn forward, landing right at the GPU's VRAM margin.
        # A fresh load defragments the CUDA pool, so force-unload and retry ONCE more
        # before giving up -- distinct from the normal CHAT_RETRIES loop above (which
        # already ran and failed identically every time for exactly this reason).
        log(f"[worker] CUDA OOM detected after normal retries -- force-unloading "
            f"{model} and retrying once more (a fresh load can defragment the CUDA "
            f"pool; a same-state retry cannot, which is why the retries above failed "
            f"identically).")
        set_keep_alive(host, model, "0")
        time.sleep(3)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            log(f"[worker] CUDA OOM recovery retry also failed: {e}")
    if resp is None:
        raise RuntimeError(f"Chat request failed after retries: {last_err_detail}") from last_err

    start = time.time()
    last_rate_write = 0.0
    last_status_write = 0.0
    token_count = 0
    think_chars = 0
    think_buf = ''
    content_parts = []
    tool_calls_acc = []
    _intra_checked_len = 0
    _pc_len = 0         # content chars streamed so far / at the last prose-runaway check
    _pc_checked = 0
    phase = None  # None -> 'thinking' -> 'writing'
    seen_checkpoints = set()
    prompt_tokens = 0
    completion_tokens = 0
    done_reason = None
    _eval_dur_ns = None

    def emit(line, color):
        if live is not None:
            live.emit(line, color)

    with resp:
        for line in _iter_stream_lines(resp):
            # Bug #1 (2026-09-18): honor an external SIGTERM pause MID-GENERATION.
            # _sigterm_pause_requested is set by the signal handler the instant the
            # queue daemon SIGTERMs us for a preemption; without this check the loop
            # would keep draining tokens until `done`, holding the lane for the whole
            # generation. Closing the response aborts the HTTP read; Ollama cancels the
            # generation on client disconnect, so the GPU frees immediately. Raising
            # drops run_task straight to its resumable-pause path (transcript through the
            # last completed iteration is already on disk).
            if _sigterm_pause_requested:
                try:
                    resp.close()
                except Exception:
                    pass
                raise ChatAbortedForPause(
                    "external SIGTERM received mid-generation -- aborted the in-flight "
                    "chat request to free the lane")
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get('message', {})
            think = msg.get('thinking', '')
            content = msg.get('content', '')
            tcs = msg.get('tool_calls') or []
            if tcs:
                # Ollama delivers tool_calls in the final message chunk(s)
                # before the done chunk; accumulate so a split delivery
                # still yields the complete list.
                tool_calls_acc.extend(tcs)

            if think:
                if phase != 'thinking':
                    emit(f'[{model}] thinking...', LIVE_YELLOW)
                    phase = 'thinking'
                think_chars += len(think)
                think_buf += think
                token_count += max(1, len(think) // 4)
                for cp in _live_find_new_checkpoints(think_buf, seen_checkpoints):
                    topic = cp if len(cp) <= 45 else cp[:42].rsplit(' ', 1)[0] + '...'
                    emit(f'[{model}] thinking about: {topic}', LIVE_YELLOW)
                    last_status_write = time.time()

                _checked, _is_loop, _intra_checked_len, _loop_reason = think_buf_loop_check(
                    think_buf, _intra_checked_len, bool(content_parts or tool_calls_acc))
                if _is_loop:
                    emit(f'[{model}] REASONING LOOP DETECTED ({_loop_reason}, '
                         f'{len(think_buf)} chars, zero content/tool_calls) -- aborting turn',
                         LIVE_YELLOW)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise _with_thinking(ChatAbortedForReasoningLoop(
                        f"intra-turn reasoning loop ({_loop_reason}): {len(think_buf)} chars of "
                        f"thinking with no content/tool_calls -- aborted the in-flight chat request"), think_buf)
                if (_REASONING_BUDGET[0] and think_chars >= _REASONING_BUDGET[0]
                        and not (content_parts or tool_calls_acc)):
                    emit(f'[{model}] REASONING RUNAWAY ({think_chars} chars of thinking, no content/'
                         f'tool_calls) -- aborting turn', LIVE_YELLOW)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise _with_thinking(ChatAbortedForReasoningRunaway(think_chars), think_buf)

            if content:
                if phase != 'writing':
                    emit(f'[{model}] thought for ~{think_chars} chars, now writing...' if think_chars
                         else f'[{model}] writing...', LIVE_YELLOW)
                    phase = 'writing'
                content_parts.append(content)
                token_count += max(1, len(content) // 4)
                _pc_len += len(content)
                if _PROSE_BUDGET[0] and not tool_calls_acc and _pc_len - _pc_checked >= PROSE_GUARD_STRIDE:
                    _pc_checked = _pc_len
                    _pc_why = prose_runaway_check(''.join(content_parts), _PROSE_BUDGET[0])
                    if _pc_why:
                        emit(f'[{model}] PROSE RUNAWAY ({_pc_why}, {_pc_len} chars, no tool call) '
                             f'-- aborting turn', LIVE_YELLOW)
                        try:
                            resp.close()
                        except Exception:
                            pass
                        raise ChatAbortedForProseRunaway(
                            _pc_why, ''.join(content_parts), think_buf, streamed=True)

            now = time.time()
            if phase == 'thinking' and now - last_status_write > 8:
                # Fallback for stretches with no bold/numbered structure to latch onto.
                emit(f'[{model}]   ...still thinking: "{_live_snippet(think_buf)}"', LIVE_DIM)
                last_status_write = now
            elif phase == 'writing' and now - last_status_write > 2:
                cur_lines = ''.join(content_parts).count(chr(10)) + 1
                emit(f'[{model}]   ...writing: line {cur_lines}', LIVE_DIM)
                last_status_write = now

            if now - last_rate_write > 0.5:
                elapsed = now - start
                rate = token_count / elapsed if elapsed > 0 else 0
                if live is not None:
                    live.write_rate(f'{model}: {rate:.1f} tok/s (est)')
                last_rate_write = now

            if d.get('done'):
                done_reason = d.get('done_reason')
                prompt_tokens = d.get('prompt_eval_count') or 0
                completion_tokens = d.get('eval_count') or 0
                _eval_dur_ns = d.get('eval_duration')
                if live is not None and completion_tokens and d.get('eval_duration'):
                    real_rate = completion_tokens / (d['eval_duration'] / 1e9)
                    live.write_rate(f'{model}: {real_rate:.1f} tok/s (last run, done)')

    full_content = ''.join(content_parts)
    full_thinking = think_buf
    elapsed = time.time() - start
    if live is not None:
        n_lines = full_content.count(chr(10)) + 1 if full_content else 0
        emit(f'[{model}] done — {n_lines} lines ({len(full_content)} chars)'
             + (f', {len(tool_calls_acc)} tool call(s)' if tool_calls_acc else '')
             + f' in {elapsed:.1f}s', LIVE_GREEN)

    # Same normalized shape call_ollama() returns for the native path:
    # message dict (role/content, plus thinking and tool_calls when the
    # model produced them -- matching what a non-streaming response
    # contains) and usage normalized to the OpenAI-style token keys.
    message = {"role": "assistant", "content": full_content}
    if full_thinking:
        message["thinking"] = full_thinking
    if tool_calls_acc:
        message["tool_calls"] = tool_calls_acc
    return {
        "streamed": True,
        "message": message,
        "done_reason": done_reason,
        "usage": usage_from_ollama({"prompt_eval_count": prompt_tokens, "eval_count": completion_tokens,
                                    "eval_duration": _eval_dur_ns}),
    }


class OpenAIStreamUnavailable(Exception):
    """Raised by call_openai_streaming when the SSE stream never produced a
    first token (connect refused/HTTP error after retries, or the endpoint
    closed/garbled the stream before any delta arrived). It means "this
    backend cannot stream right now", NOT "this turn failed": run_task
    catches it and re-issues the identical turn through the blocking
    call_ollama path, so --live-log can never turn a working dispatch into a
    broken one. Once the first delta has arrived the stream is authoritative
    and a later failure is surfaced as a normal RuntimeError chat failure
    instead -- re-sending a partially-consumed generation would duplicate it."""


def call_openai_streaming(host: str, model: str, messages: list, temperature: float, num_ctx: int,
                          timeout: int = CHAT_TIMEOUT_S, tools: bool = True,
                          top_p: float = None, top_k: int = None, live: "LiveLog" = None,
                          max_tokens: int = None,
                          repeat_penalty: float = None,
                          preserve_reasoning: bool = False, think=None,
                          role: str = "author", sampling_step: int = 0, seed: int = None) -> dict:
    """Streaming counterpart of call_ollama()'s api_style="openai" branch, for
    the Darkbloom local endpoint (/v1/chat/completions) and any other
    OpenAI-compatible server. Added 2026-10-01: after every Studio dispatch
    moved to Darkbloom (--api openai forced), --live-log went dead -- live
    progress, tok/s and the dashboard's per-iteration token line only ever
    existed on the native Ollama /api/chat path (call_ollama_streaming).

    Sends the SAME payload the non-streaming openai branch sends (model,
    messages, temperature, top_p, max_tokens, repeat_penalty, tools=TOOLS)
    plus stream: true and stream_options.include_usage; if the server rejects
    stream_options it is dropped and token counts are estimated from the
    stream instead (one un-charged extra attempt). Parses `data: {...}` SSE
    lines including the `data: [DONE]` sentinel, accumulates content,
    reasoning_content/reasoning and tool_call deltas (which arrive split
    across chunks, arguments a fragment at a time), and emits exactly the
    live-log lines call_ollama_streaming emits so the dashboard's progress
    parser is unchanged.

    Returns the same normalized shape as call_ollama: {"message": {...},
    "usage": {prompt_tokens, completion_tokens, total_tokens}} with the
    message left in OpenAI shape (tool_calls carrying id/type and
    `arguments` as a JSON STRING, which run_task's tool loop already
    accepts), and the same OPT-IN reasoning_content -> thinking promotion the
    non-streaming branch does under --preserve-reasoning."""
    _f = _profile_fields(model, role, "openai", temperature, num_ctx, top_p, top_k,
                         max_tokens, repeat_penalty, think,
                         sampling_step=sampling_step, seed=seed, host=host)
    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)
    payload = {"model": model, "messages": messages, "stream": True}
    payload.update(_f)
    if tools:
        payload["tools"] = TOOLS
    url = f"{host}/v1/chat/completions"

    # --- connect phase: retried exactly like call_ollama (same CHAT_RETRIES,
    # same 429/502/503/504 backoff). The Request is rebuilt per attempt because
    # Darkbloom rotates its API key on every provider restart -- a Request built
    # once would re-send a stale key on every retry.
    resp = None
    last_err = None
    last_err_detail = None
    want_usage = True
    attempt = 0
    budget = CHAT_RETRIES + 1
    _lane_state = {}
    while attempt < budget:
        attempt += 1
        if want_usage:
            payload["stream_options"] = {"include_usage": True}
        else:
            payload.pop("stream_options", None)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     **_openai_auth_headers(host)},
            method="POST",
        )
        _t0 = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            _conf_rec(host, "/v1/chat/completions", "openai", model, payload, req, role, 200, _t0, step=sampling_step)
            break
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                body = "<could not read response body>"
            _conf_rec(host, "/v1/chat/completions", "openai", model, payload, req, role, e.code, _t0, step=sampling_step)
            if want_usage and "stream_options" in body.lower():
                # The endpoint doesn't know stream_options (older llama-server,
                # strict schema validation). Drop it and retry WITHOUT charging the
                # attempt against CHAT_RETRIES -- this is a capability downgrade, not
                # a transient failure, and it must not eat the real retry budget.
                want_usage = False
                budget += 1
                log("[worker] endpoint rejected stream_options.include_usage -- retrying "
                    "without it (token counts will be estimated from the stream).")
                continue
            last_err = e
            last_err_detail = f"{e} -- body: {body}"
            if _lane_restart_retry(host, e.code, last_err_detail, _lane_state, model=model):
                budget += 1       # lane restarted and is back: not charged
                continue
            if attempt < budget:
                log(f"[worker] Darkbloom streaming request failed ({last_err_detail}), "
                    f"retry {attempt}/{CHAT_RETRIES}...")
                if e.code in (429, 502, 503, 504):
                    # Darkbloom's slots are shared with fleet traffic: at the cap it
                    # answers 429/503. Same backoff as call_ollama.
                    time.sleep(min(30, 5 * attempt))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            last_err_detail = str(e)
            _conf_rec(host, "/v1/chat/completions", "openai", model, payload, req, role, None, _t0, step=sampling_step)
            if (not isinstance(e, TimeoutError)
                    and _lane_restart_retry(host, None, last_err_detail, _lane_state)):
                budget += 1
                continue
            if attempt < budget:
                log(f"[worker] Darkbloom streaming request failed ({e}), "
                    f"retry {attempt}/{CHAT_RETRIES}...")
    if resp is None:
        raise OpenAIStreamUnavailable(
            f"streaming connect failed after retries: {last_err_detail}") from last_err

    start = time.time()
    last_rate_write = 0.0
    last_status_write = 0.0
    token_count = 0
    think_chars = 0
    think_buf = ''
    content_parts = []
    tc_slots = {}       # index -> OpenAI tool_call dict being assembled
    tc_order = []       # first-seen order of those indices
    _intra_checked_len = 0
    _pc_len = 0         # content chars streamed so far / at the last prose-runaway check
    _pc_checked = 0
    phase = None        # None -> 'thinking' -> 'writing'
    seen_checkpoints = set()
    prompt_tokens = 0
    completion_tokens = 0
    done_reason = None
    saw_first_delta = False
    saw_terminator = False

    def emit(line, color):
        if live is not None:
            live.emit(line, color)

    def _fail(detail, exc=None):
        """Before the first delta a stream failure is recoverable (run_task
        re-runs the turn non-streaming); after it, it is a real chat failure."""
        if saw_first_delta:
            raise RuntimeError(
                f"Darkbloom stream failed mid-generation: {detail}") from exc
        raise OpenAIStreamUnavailable(f"stream failed before first token: {detail}") from exc

    try:
        with resp:
            for raw_line in _iter_stream_lines(resp):
                # Honor an external SIGTERM pause MID-GENERATION, exactly as the native
                # streaming path does (see ChatAbortedForPause): closing the response
                # aborts the HTTP read, the server cancels the generation on client
                # disconnect, and run_task drops to its resumable-pause save path.
                if _sigterm_pause_requested:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise ChatAbortedForPause(
                        "external SIGTERM received mid-generation -- aborted the in-flight "
                        "chat request to free the lane")
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue  # keep-alive comment / SSE event separator
                if not line.startswith("data:"):
                    continue  # `event:`/`id:` fields carry nothing we need
                data = line[5:].strip()
                if data == "[DONE]":
                    saw_terminator = True
                    break
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                if isinstance(d.get("usage"), dict):
                    # With include_usage the final chunk carries real counts (and an
                    # empty choices list); some servers also repeat usage per chunk.
                    _u = d["usage"]
                    prompt_tokens = _u.get("prompt_tokens") or prompt_tokens
                    completion_tokens = _u.get("completion_tokens") or completion_tokens
                choices = d.get("choices") or []
                if not choices:
                    continue
                ch = choices[0] or {}
                # `delta` on a stream; `message` tolerated so a server that answers a
                # stream request non-incrementally still works instead of looking empty.
                delta = ch.get("delta") or ch.get("message") or {}
                if ch.get("finish_reason"):
                    done_reason = ch["finish_reason"]
                    saw_terminator = True
                content = delta.get("content") or ''
                think = delta.get("reasoning_content") or delta.get("reasoning") or ''
                tcds = delta.get("tool_calls") or []
                if content or think or tcds:
                    saw_first_delta = True

                for tcd in tcds:
                    # Tool calls stream as fragments: the first chunk usually carries
                    # index+id+function.name, later chunks only index +
                    # function.arguments (a few characters each). Accumulate per index.
                    idx = tcd.get("index")
                    if idx is None:
                        idx = tc_order[-1] if tc_order else 0
                    if idx not in tc_slots:
                        tc_slots[idx] = {"id": None, "type": "function",
                                         "function": {"name": "", "arguments": ""}}
                        tc_order.append(idx)
                    slot = tc_slots[idx]
                    if tcd.get("id"):
                        slot["id"] = tcd["id"]
                    if tcd.get("type"):
                        slot["type"] = tcd["type"]
                    fn = tcd.get("function") or {}
                    _name = fn.get("name")
                    if _name and _name != slot["function"]["name"]:
                        # Normally sent once, whole; concatenate only genuinely new
                        # text so a server that repeats the full name each chunk
                        # doesn't produce "read_fileread_file".
                        slot["function"]["name"] += _name
                    _args = fn.get("arguments")
                    if _args:
                        slot["function"]["arguments"] += _args

                if think:
                    if phase != 'thinking':
                        emit(f'[{model}] thinking...', LIVE_YELLOW)
                        phase = 'thinking'
                    think_chars += len(think)
                    think_buf += think
                    token_count += max(1, len(think) // 4)
                    for cp in _live_find_new_checkpoints(think_buf, seen_checkpoints):
                        topic = cp if len(cp) <= 45 else cp[:42].rsplit(' ', 1)[0] + '...'
                        emit(f'[{model}] thinking about: {topic}', LIVE_YELLOW)
                        last_status_write = time.time()

                    _checked, _is_loop, _intra_checked_len, _loop_reason = think_buf_loop_check(
                        think_buf, _intra_checked_len, bool(content_parts or tc_slots))
                    if _is_loop:
                        emit(f'[{model}] REASONING LOOP DETECTED ({_loop_reason}, '
                             f'{len(think_buf)} chars, zero content/tool_calls) -- aborting turn',
                             LIVE_YELLOW)
                        try:
                            resp.close()
                        except Exception:
                            pass
                        raise _with_thinking(ChatAbortedForReasoningLoop(
                            f"intra-turn reasoning loop ({_loop_reason}): {len(think_buf)} chars of "
                            f"thinking with no content/tool_calls -- aborted the in-flight chat request"), think_buf)
                    if (_REASONING_BUDGET[0] and think_chars >= _REASONING_BUDGET[0]
                            and not (content_parts or tc_slots)):
                        emit(f'[{model}] REASONING RUNAWAY ({think_chars} chars of thinking, no '
                             f'content/tool_calls) -- aborting turn', LIVE_YELLOW)
                        try:
                            resp.close()
                        except Exception:
                            pass
                        raise _with_thinking(ChatAbortedForReasoningRunaway(think_chars), think_buf)

                if content:
                    if phase != 'writing':
                        emit(f'[{model}] thought for ~{think_chars} chars, now writing...' if think_chars
                             else f'[{model}] writing...', LIVE_YELLOW)
                        phase = 'writing'
                    content_parts.append(content)
                    token_count += max(1, len(content) // 4)
                    _pc_len += len(content)
                    if _PROSE_BUDGET[0] and not tc_slots and _pc_len - _pc_checked >= PROSE_GUARD_STRIDE:
                        _pc_checked = _pc_len
                        _pc_why = prose_runaway_check(''.join(content_parts), _PROSE_BUDGET[0])
                        if _pc_why:
                            emit(f'[{model}] PROSE RUNAWAY ({_pc_why}, {_pc_len} chars, no tool call) '
                                 f'-- aborting turn', LIVE_YELLOW)
                            try:
                                resp.close()
                            except Exception:
                                pass
                            raise ChatAbortedForProseRunaway(
                                _pc_why, ''.join(content_parts), think_buf, streamed=True)

                now = time.time()
                if phase == 'thinking' and now - last_status_write > 8:
                    emit(f'[{model}]   ...still thinking: "{_live_snippet(think_buf)}"', LIVE_DIM)
                    last_status_write = now
                elif phase == 'writing' and now - last_status_write > 2:
                    cur_lines = ''.join(content_parts).count(chr(10)) + 1
                    emit(f'[{model}]   ...writing: line {cur_lines}', LIVE_DIM)
                    last_status_write = now

                if now - last_rate_write > 0.5:
                    elapsed = now - start
                    rate = token_count / elapsed if elapsed > 0 else 0
                    if live is not None:
                        live.write_rate(f'{model}: {rate:.1f} tok/s (est)')
                    last_rate_write = now
    except (ChatAbortedForPause, ChatAbortedForReasoningLoop, ChatAbortedForReasoningRunaway,
            ChatAbortedForProseRunaway):
        raise
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError,
            ValueError) as e:
        # Mid-stream disconnect (provider restart, killed slot, truncated chunked
        # body). Before the first delta this is recoverable; after it we must NOT
        # silently return a half-generation -- partial tool-call arguments would be
        # invalid JSON and a partial answer could be mistaken for a final one.
        _fail(str(e), e)

    if done_reason == "error" and not tc_slots:
        # Darkbloom (MTP + tool-call generation) reports a server-side generation
        # failure as finish_reason:"error" ("Response generation failed") -- the same
        # deterministic failure the non-streaming path reports as HTTP 422 "Inference
        # generation failed". Surface it as that error so run_task's 422 recovery
        # ladder (_recover_422) handles both lanes identically.
        raise RuntimeError("Chat request failed after retries: HTTP Error 422: Unprocessable "
                           "Entity -- body: Response generation failed (stream finish_reason=error)")

    if not saw_terminator:
        # The body ended without `data: [DONE]` and without a finish_reason. A
        # truncated chunked/Content-Length body does NOT raise in http.client -- it
        # silently stops iterating (see its readinto: "Ideally, we would raise
        # IncompleteRead ... but it might break compatibility") -- so without this
        # check a provider restart mid-generation would look like a clean, complete
        # short answer and could be accepted as the model's final word.
        _fail("stream ended without [DONE]/finish_reason (truncated body -- the "
              "provider or slot went away mid-generation)")

    full_content = ''.join(content_parts)
    tool_calls_acc = []
    for idx in tc_order:
        slot = tc_slots[idx]
        if not slot["id"]:
            # OpenAI-style tool results must carry tool_call_id; synthesize a stable
            # one if the server streamed the call without an id.
            slot["id"] = f"call_{idx}"
        tool_calls_acc.append(slot)

    if not (full_content.strip() or think_buf.strip() or tool_calls_acc):
        # An empty stream is indistinguishable from a broken one and run_task's
        # empty-answer nudge is a scarce resource -- fall back rather than spend it.
        raise OpenAIStreamUnavailable("stream closed without producing any content, "
                                      "reasoning or tool call")

    elapsed = time.time() - start
    if live is not None:
        n_lines = full_content.count(chr(10)) + 1 if full_content else 0
        emit(f'[{model}] done — {n_lines} lines ({len(full_content)} chars)'
             + (f', {len(tool_calls_acc)} tool call(s)' if tool_calls_acc else '')
             + f' in {elapsed:.1f}s', LIVE_GREEN)

    message = {"role": "assistant", "content": full_content}
    _strip_inline_think(message)
    full_content = message["content"]
    if tool_calls_acc:
        message["tool_calls"] = tool_calls_acc
    # Same OPT-IN promotion the non-streaming openai branch does (see call_ollama):
    # thinking models deliver their text in reasoning_content, and the content
    # fallback is narrow on purpose -- raw chain-of-thought is only promoted when the
    # turn would otherwise be completely empty.
    if preserve_reasoning and think_buf:
        message["thinking"] = think_buf
        # REASONING_PARSER on: a turn whose reasoning was cut at the output cap stays a reasoning-only turn (run_task's
        # think-cap recovery); promoting it into content would turn the chain of thought into "prose".
        if (not full_content.strip() and not tool_calls_acc
                and not ("reasoning_parser" in payload and str(done_reason or "").lower() == "length")):
            message["content"] = think_buf

    if not completion_tokens:
        # No include_usage (or the server sent none): estimate, because the
        # dashboard's tok/s line is gated on a non-zero completion count. ~4 chars
        # per token, the same ratio the live rate display above uses.
        completion_tokens = max(1, (len(full_content) + len(think_buf)
                                    + sum(len(json.dumps(tc)) for tc in tool_calls_acc)) // 4)
        log(f"[worker] streamed response carried no usage block -- estimating "
            f"{completion_tokens} completion tokens from the stream.")
    if not prompt_tokens:
        try:
            prompt_tokens = max(1, sum(len(str(m.get("content") or "")) for m in messages) // 4)
        except Exception:
            prompt_tokens = 0
    return {
        "streamed": True,
        "message": message,
        "done_reason": done_reason,
        **({"_reasoning_parser": True,
            "_thinking_on": (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is not False}
           if "reasoning_parser" in payload else {}),
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


def set_keep_alive(host: str, model: str, keep_alive: str) -> None:
    """Adjust how long a model stays resident after this dispatch ends,
    without generating anything -- a bare /api/generate with no `prompt`
    just applies the new `keep_alive` TTL to the already-loaded model.
    Ollama's own default keep_alive is 5m, which would otherwise unload the
    model almost immediately after the last real request in the loop below.
    Added 2026-08-28 alongside pick_host(): now that dispatch fits a model
    to whichever host actually has room for it, it's worth leaving it
    resident there for reuse instead of paying a full cold-load again on
    the very next dispatch."""
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "keep_alive": keep_alive}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        log(f"[worker] set keep_alive={keep_alive} for {model} on {host}.")
    except Exception as e:
        log(f"[worker] WARNING: failed to set keep_alive for {model} on {host}: {e}")


def _try_lan_copy(host: str, model: str) -> bool:
    """Before falling back to a fresh registry pull, try copying the model
    over the LAN instead -- from Unraid's shared store if this dispatch
    targets Mac Studio's local Ollama, or from Mac Studio's local cache if
    this dispatch targets Unraid. Mirrors bakeoff-driver-buildoff.sh's
    ensure_model_available(), generalized here so it applies to any
    dispatch (ad-hoc or driver-orchestrated), not only driver-orchestrated
    ones -- confirmed live 2026-08-22 that a direct ollama-worker.py
    invocation skipped this safeguard entirely before this fix, since it
    previously only lived in the bash driver wrapping this function.
    Returns True if the copy succeeded (model is now present at `host`),
    False if the LAN path isn't available or the model isn't there either
    -- caller falls back to a fresh pull in that case."""
    if not os.path.ismount(LAN_MOUNT_ROOT):
        log(f"[worker] {LAN_MOUNT_ROOT} not mounted -- skipping LAN-copy path.")
        return False

    targets_unraid = any(h in host for h in _unraid_host_substrings())
    direction = "push" if targets_unraid else "pull"
    log(f"[worker] {model} not present at {host} -- trying LAN copy ({direction}) before a fresh pull...")

    try:
        result = subprocess.run(
            [sys.executable, COPY_HELPER, direction, model],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as e:
        log(f"[worker] LAN copy failed to run: {e}")
        return False

    if result.returncode != 0:
        log(f"[worker] LAN copy unsuccessful ({direction} {model}): {(result.stdout or '').strip()} {(result.stderr or '').strip()}")
        return False

    log(f"[worker] LAN copy succeeded ({direction} {model}).")
    return True


# ZERO CPU spillover tolerated on Unraid -- the owner, 2026-08-28, in these exact
# words, after a dispatch was reported as "should clear, ~10% spillover,
# under threshold": "no spillover, at all, period." A percentage-threshold
# framing (this constant used to be SPILLOVER_ABORT_FRACTION = 0.15) reads
# as "up to 15% is fine," which is not the rule and got restated back to
# The owner as if it were -- the standing rule has always been zero, going back
# to "no. no cpu spillover on unraid." earlier this same session. The
# post-warmup check below now aborts on ANY measured spillover at all, no
# threshold, no epsilon -- size/size_vram from /api/ps are exact integer
# byte counts, not noisy floats, so there is nothing to buffer against.


def _host_usable_bytes(host: str) -> int | None:
    """usable_bytes for `host` from the configured host table, matched by URL,
    or None if this isn't a configured host OR is configured without a budget
    (an unrecognized/unmeasured host has no data to check against, so the
    spillover checks below just skip rather than block)."""
    for spec in load_ollama_hosts().values():
        if spec["url"] == host:
            return spec["usable_bytes"]
    return None


def _usable_prime(msgs) -> bool:
    """True if `msgs` is a real [system, task] prefix worth warming the cache with.

    Deliberately strict and cheap: this runs on the path that every dispatch
    goes through, so anything it cannot vouch for falls back to the old
    minimal probe rather than risking the warmup. Requires a non-empty list of
    dicts that each carry a role and non-empty string content.
    """
    if not isinstance(msgs, list) or not msgs:
        return False
    for m in msgs:
        if not isinstance(m, dict):
            return False
        if not m.get("role") or not isinstance(m.get("content"), str) or not m["content"].strip():
            return False
    return True



PREFLIGHT_MAX_TOKENS = 256   # the tool-calling probe only needs the server to accept TOOLS


def _preflight_timeout_default():
    """30s unless WORKER_PREFLIGHT_TIMEOUT_S is set (a slow-prefill server such as the Strata
    expert-offload engine needs minutes for the tools-bearing first request)."""
    try:
        v = int(os.environ.get("WORKER_PREFLIGHT_TIMEOUT_S", "") or 30)
    except ValueError:
        return 30
    return v if v > 0 else 30


def _tool_calling_preflight(host, model, urlopen=None, sleep=time.sleep):
    """One minimal real request carrying TOOLS, so a server that cannot tool-call
    fails HERE, in seconds, before run_task's loop spends context.

    DARKBLOOM LANE (2026-10-03, cold-load support): Darkbloom hosts more models
    than are resident and LAZY-LOADS a hosted model on its first request. This is
    that first request, so on the Darkbloom lane it gets WARMUP_TIMEOUT_S (a
    multi-GB weight load), a slot-busy refusal / provider restart is waited out
    via _lane_restart_retry (uncharged, bounded), and a 5xx is reported as a
    SERVER error -- never as "no tool-calling support" (Darkbloom always speaks
    tools; the --jinja diagnosis is a llama-server one). Every other host keeps
    the original 30s / any-500-means-no-jinja behaviour byte for byte."""
    urlopen = urlopen or urllib.request.urlopen
    darkbloom = _is_darkbloom_lane(host)
    timeout = WARMUP_TIMEOUT_S if darkbloom else _preflight_timeout_default()
    if darkbloom:
        log(f"[worker] preflight on the Darkbloom lane: first request to {model} may "
            f"lazy-load it (cold start) -- allowing up to {WARMUP_TIMEOUT_S}s.")
    state = {}
    while True:
        _pf_body = {
            "model": model,
            "messages": [{"role": "user", "content": "ready"}],
            "tools": TOOLS,
            "stream": False,
            # a capability probe, not a generation: the model's review (non-thinking)
            # profile so it never spends the cold-load request on a thinking trace,
            # with a small explicit max_tokens (override).
            **_profile_fields(model, "review", "openai", max_tokens=PREFLIGHT_MAX_TOKENS),
        }
        preflight_req = urllib.request.Request(
            f"{host}/v1/chat/completions",
            data=json.dumps(_pf_body).encode(),
            headers={"Content-Type": "application/json",
                     **_openai_auth_headers(host)},
            method="POST",
        )
        _t0 = time.monotonic()
        try:
            with urlopen(preflight_req, timeout=timeout) as resp:
                _pf_raw = resp.read()
            try:
                _pf_json = json.loads(_pf_raw)
            except Exception:
                _pf_json = None
            _conf_rec(host, "/v1/chat/completions", "openai", model, _pf_body, preflight_req, "review", 200, _t0, raw=_pf_json)
            return
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                body = "<could not read response body>"
            _conf_rec(host, "/v1/chat/completions", "openai", model, _pf_body, preflight_req, "review", e.code, _t0)
            if darkbloom:
                if _lane_restart_retry(host, e.code, f"{e} -- body: {body}", state,
                                       sleep=sleep, model=model):
                    continue
                kind = ("server error" if 500 <= int(e.code) <= 599
                        else "rejected the request")
                raise RuntimeError(
                    f"Darkbloom at {host} {kind} on the preflight request for "
                    f"{model!r} (HTTP {e.code}: {body}). This is NOT a tool-calling "
                    f"capability verdict -- check BloomGauge (model served? slots? "
                    f"memory?); BloomGauge runs Darkbloom, never run darkbloom by hand."
                ) from e
            if e.code == 500:
                # ANY 500 from this specific preflight call is treated
                # as "no tool-calling support" -- the confirmed wording
                # mentions --jinja, but we don't want a slightly
                # different error string to slip past and surface
                # mid-dispatch.
                raise RuntimeError(
                    f"llama-server at {host} does not support tool-calling "
                    f"(preflight check failed: {body}). If this is Ollama's own "
                    f"internal backend, it was started without --jinja. Use Darkbloom "
                    f"instead (BloomGauge runs it; never start it by hand), then retry with "
                    f"--host {DEFAULT_OPENAI_HOST}."
                ) from e
            raise RuntimeError(
                f"llama-server at {host} rejected the tool-calling preflight "
                f"request (HTTP {e.code}: {body})."
            ) from e
        except (urllib.error.URLError, ConnectionError) as e:
            _conf_rec(host, "/v1/chat/completions", "openai", model, _pf_body, preflight_req, "review", None, _t0)
            if darkbloom and _lane_restart_retry(host, None, str(e), state, sleep=sleep):
                continue
            raise RuntimeError(
                f"tool-calling preflight request to {host}/v1/chat/completions "
                f"failed: {e}"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"tool-calling preflight request to {host}/v1/chat/completions "
                f"failed: {e}"
            ) from e


def _inventory_lists_missing_model(host: str, model: str, err: BaseException) -> bool:
    """True when `err` is the host saying the model does not exist (HTTP 404 / "not found") while
    the shared model inventory still lists it on `host` -- i.e. the cached list is stale because
    the model was removed outside this queue."""
    code = getattr(err.__cause__, "code", None)
    text = str(err).lower()
    if not (code == 404 or ("404" in text and "not found" in text)):
        return False
    with _INV_LOCK:
        ent = _inv_get(_inv_key(host))
    return ent is not None and _normalize_model_name(model) in ent["models"]


def ensure_model_ready(host: str, model: str, temperature: float, num_ctx: int,
                        top_p: float = None, top_k: int = None, api_style: str = "ollama",
                        manual_tools: bool = False, prime_messages: list = None) -> None:
    """Confirm the model is pulled and load it -- every rule is in _ensure_model_ready_once.

    This wrapper is the safety net for the shared model inventory (host_model_inventory): a
    cached list can say a model is present after it was removed outside this queue (`ollama rm`
    by hand), and the warmup then fails with an HTTP 404. Before the inventory, the per-call
    /api/tags caught that and pulled. So on a 404/"not found" while the cached list still lists
    the model, forget the list and run the whole check once more: it re-reads the host, sees the
    model is gone, and pulls it. Exactly one retry; any other failure propagates untouched."""
    kw = dict(top_p=top_p, top_k=top_k, api_style=api_style, manual_tools=manual_tools,
              prime_messages=prime_messages)
    try:
        return _ensure_model_ready_once(host, model, temperature, num_ctx, **kw)
    except RuntimeError as e:
        if api_style == "openai" or not _inventory_lists_missing_model(host, model, e):
            raise
        log(f"[worker] {host} reports {model} missing ({e}) although the cached model list "
            f"has it -- the list is stale (model removed outside the queue?). Re-reading it "
            f"and re-checking once.")
        invalidate_host_inventory(host)
    return _ensure_model_ready_once(host, model, temperature, num_ctx, **kw)


def _ensure_model_ready_once(host: str, model: str, temperature: float, num_ctx: int,
                             top_p: float = None, top_k: int = None, api_style: str = "ollama",
                             manual_tools: bool = False, prime_messages: list = None) -> None:
    """Confirm the model is pulled, then explicitly load it into memory with
    a generous timeout, fully separate from the main dispatch loop's
    request timeout. A cold model load (reading multi-GB weights off disk)
    can easily exceed a normal chat-request timeout on its own, before any
    real work even starts -- this was the actual cause of an earlier crash
    tonight (a plain 180s timeout on the very first request to a model
    that hadn't been loaded yet).

    Also enforces host/model fit -- added 2026-08-28 after dispatching
    gpt-oss:20b to Unraid with an explicit --host, which bypasses
    pick_host()'s fit check entirely (that check only runs when --host is
    omitted). Confirmed live: gpt-oss:20b's base weights (13.79GB) already
    exceed Unraid's 12GB card before any context is even added, and at
    --num-ctx 65536 it loaded at ~10GB VRAM / ~22.8GB total -- over half
    spilled to CPU. The owner: "we need hard rules programmed for model
    dispatching to prevent this. it keeps happening." Confirmed unconditional,
    no override -- the owner: "no. no cpu spillover on unraid." This is that hard
    rule, made non-bypassable by living here (every dispatch calls this,
    regardless of how --host was chosen) rather than only in pick_host()
    (which an explicit --host skips past), and by having no escape hatch at
    all. Two checks, both real data:
    pre-flight (the model's own advertised size vs. the host's usable
    budget, before even attempting a load) and post-warmup (the ACTUAL
    measured VRAM/total split from the host's own /api/ps, not an
    estimate) -- the second one is what would have caught this specific
    case, since the pre-flight size alone doesn't account for --num-ctx's
    contribution to the loaded footprint.

    Liveness is GET /api/version; presence and the base size come from the shared
    per-host model inventory (host_model_inventory), NOT a per-dispatch /api/tags
    (on Ollama 0.40.x that parses GGUF metadata for every model off the array disks).
    A model that looks missing forces one re-read before anything is pulled, and a
    LAN copy or pull invalidates the list afterwards."""
    if api_style == "openai":
        # llama-server loads exactly one model, given via -m at process
        # startup -- there's no /api/tags-style discovery or /api/pull, and
        # nothing to warm up (the model is either already resident because
        # the server started successfully, or the server isn't up at all).
        log(f"[worker] checking llama-server is up at {host}...")
        req = urllib.request.Request(f"{host}/health")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # Connection refused/timeout = nothing is listening at all --
            # a different failure from a server that IS up but broken (that
            # one answers /health fine and gets caught by the tool-support
            # preflight below).
            if host == DEFAULT_OPENAI_HOST:
                raise RuntimeError(
                    f"Nothing is listening on {host} -- Darkbloom's local endpoint is down. "
                    f"BloomGauge runs Darkbloom (the queue's BloomGauge control starts it for "
                    f"queued work); never run darkbloom by hand. Needs the owner if it stays down."
                ) from e
            raise RuntimeError(
                f"could not connect to {host}/health ({e}) -- is a server "
                f"running there?"
            ) from e
        except Exception as e:
            raise RuntimeError(f"could not reach {host}/health: {e}") from e
        log(f"[worker] llama-server is up.")
        if not manual_tools:
            # Tool-support preflight (added 2026-08-28): a llama-server
            # started WITHOUT --jinja -- e.g. Ollama's own internal backend,
            # which runs with --no-jinja -- rejects ANY request carrying
            # `tools` with a deterministic 500:
            # {"error":{"message":"tools param requires --jinja flag",...}}.
            # /health alone can't catch that: the server is perfectly
            # healthy, it just can't do tool-calling. Confirmed live
            # 2026-08-28: a dispatch that grepped `ps aux` for "any"
            # llama-server grabbed Ollama's internal backend and only hit
            # this error mid-run, after real context had already been
            # spent. Send one minimal real request with the actual TOOLS
            # schema so this fails in seconds HERE, before run_task's main
            # loop ever starts. Skipped under --manual-tools: that path
            # never sends `tools` at all, so the check would be pointless
            # overhead.
            log(f"[worker] preflight: verifying {host} accepts tool-calling requests...")
            _tool_calling_preflight(host, model)
            log(f"[worker] preflight OK: {host} accepts tool-calling requests.")
        return

    # Hard rule (2026-10-04): a Darkbloom-only model id on an Ollama host is a
    # ROUTING bug, never a missing model -- it is on no Ollama registry, so the
    # LAN-copy + /api/pull below can only fail (HTTP 500 traceback on Unraid:
    # 27de99a9d14c, 1977f33a6919, 759f2ebc2ea1). Fail fast, before any pull.
    if _is_darkbloom_only_model(model):
        raise RuntimeError(
            f"refusing to pull {model!r} on Ollama host {host}: it is a Darkbloom-only "
            f"model (served by the local Darkbloom endpoint, absent from every Ollama "
            f"registry). The job was routed to the wrong lane -- it belongs on the "
            f"Darkbloom lane ({_darkbloom_default_host()}); check ~/.darkbloom/local.json "
            f"and the queue's _candidate_lanes.")

    # Hard rule, no override (same pattern as the Unraid-spillover rule
    # above): a TEMPLATE_BUG_MODELS model on Studio's native Ollama crashes
    # AND, if the dedicated bypass server for it is already resident, silently
    # double-loads the same multi-GB weights into unified memory a second
    # time -- confirmed live 2026-08-29, see TEMPLATE_BUG_MODELS' comment.
    # DEFAULT_OPENAI_HOST is always loopback, so it only ever collides with a
    # loopback native-Ollama host (Studio) -- Unraid's native Ollama is a
    # different physical machine and has no bypass server to collide with.
    if model in TEMPLATE_BUG_MODELS and host in ("http://127.0.0.1:11434", "http://localhost:11434"):
        raise RuntimeError(
            f"{model} is in TEMPLATE_BUG_MODELS -- Ollama's native /api/chat crashes it "
            f"(github.com/ollama/ollama/issues/17778) and routing it here risks double-"
            f"loading the same weights alongside the dedicated bypass server. Use "
            f"--api openai --host {DEFAULT_OPENAI_HOST} instead (Darkbloom; BloomGauge runs it, "
            f"never start it by hand). No override."
        )

    log(f"[worker] checking {model} is pulled on {host}...")
    # Liveness is /api/version (constant time). /api/tags is NOT cheap on Ollama 0.40.x --
    # it parses GGUF metadata for every pulled model off the array disks -- so presence and
    # base size come from the shared inventory, read from the network at most once per
    # OLLAMA_INVENTORY_TTL_S (see host_model_inventory).
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{host}/api/version"),
                                    timeout=10) as resp:
            resp.read()
    except Exception as e:
        raise RuntimeError(f"could not reach {host}/api/version: {e}") from e
    _t_call = _inv_now()
    ent = _inventory_entry(host, timeout=30, ignore_failure_memo=True)
    if ent is not None and _normalize_model_name(model) not in ent["models"] \
            and ent["fetched_at"] < _t_call:
        # Looks missing -- but this list predates the call (it came from the cache), so it may predate
        # a pull made outside this queue. Re-read it once before deciding to pull anything. (A list
        # read from the host during this very call is already that re-read.)
        ent = _inventory_entry(host, force=True, timeout=30, ignore_failure_memo=True)
    if ent is None:
        raise RuntimeError(f"could not reach {host}/api/tags: {_inv_last_error(host)}")
    inventory = dict(ent["models"])

    usable = _host_usable_bytes(host)
    if usable is not None:
        target = _normalize_model_name(model)
        base_size = inventory.get(target)
        if (base_size and base_size > usable
                and target not in UNRAID_SPILLOVER_EXCEPTIONS
                and model not in UNRAID_SPILLOVER_EXCEPTIONS):
            raise RuntimeError(
                f"{model} ({base_size/1e9:.1f}GB) already exceeds {host}'s usable budget "
                f"({usable/1e9:.1f}GB) from base weights alone, before any --num-ctx overhead. "
                f"This host cannot fit this model -- no override. Use pick_host()'s auto-selection "
                f"(omit --host) or pick a different host."
            )
        # A UNRAID_SPILLOVER_EXCEPTIONS model skips this conservative base-size
        # gate on purpose -- it's allowed to exceed usable_bytes here because
        # the real enforcement is the measured post-warmup spillover check
        # below, which is accurate where this pre-estimate is just a guess.

    # Ollama's /api/tags always qualifies a tag-less model with ":latest"
    # (e.g. "qwen3-14b-agentic" is listed as "qwen3-14b-agentic:latest"),
    # but a caller passing the bare name (no ":" at all) never matches that
    # literally -- confirmed live 2026-08-21: this false "not present"
    # triggered a real /api/pull against the public registry for a
    # locally-built custom model with no upstream equivalent, which 500'd
    # and crashed the whole dispatch. Normalize both sides to name:tag
    # (defaulting a missing tag to "latest") before comparing.
    # (inventory keys are already normalized to name:tag)
    if _normalize_model_name(model) not in inventory:
        # Before ever pulling fresh from the public registry, try a LAN copy
        # first. This safeguard already existed in bakeoff-driver-buildoff.sh
        # (ensure_model_available()) but only for driver-orchestrated runs --
        # confirmed live 2026-08-22 that any ad-hoc direct dispatch (calling
        # this script by hand, not through a driver) skipped it entirely,
        # since the check lived in bash wrapping this function rather than in
        # the function itself. Moving it here makes it apply universally.
        try:
            if not _try_lan_copy(host, model):
                log(f"[worker] {model} not present locally or on the LAN -- pulling fresh from the registry (this can take a while for a large model)...")
                pull_req = urllib.request.Request(
                    f"{host}/api/pull",
                    data=json.dumps({"model": model, "stream": False}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(pull_req, timeout=1800) as resp:
                    resp.read()
                log(f"[worker] pull complete.")
        finally:
            # A LAN copy or a pull changes the host's model list (and a half-done one may have):
            # forget the cached list so the next lookup re-reads it.
            invalidate_host_inventory(host)

    # WARM UP WITH THE REAL PREFIX, NOT A THROWAWAY (2026-09-19).
    #
    # This used to send [{"role":"user","content":"ready"}]. That loads the
    # weights -- which is all it was written to do -- but it also leaves the
    # slot holding a prompt that shares NO prefix with the conversation about
    # to start, so iteration 1 of every job paid a full cold prefill even when
    # the model was already resident with a compatible prefix.
    #
    # Sending the run's actual [system, task] pair instead validates exactly
    # the same things (weights load, host answers, template renders) AND
    # leaves the KV/prefix cache primed with the precise bytes turn 1 needs.
    # max_tokens=1 keeps it a prefill, not a generation: we are buying the
    # cache, not an answer, and the content of the reply is discarded either
    # way.
    #
    # Safety: prime_messages is OPTIONAL and falls back to the old "ready"
    # probe. A caller that does not pass it (or passes something malformed)
    # gets exactly the previous behaviour, so this cannot become a new way for
    # warmup -- and therefore every dispatch -- to fail.
    prime = prime_messages if _usable_prime(prime_messages) else [
        {"role": "user", "content": "ready"}]
    log(f"[worker] warming up {model} (loading into memory, up to {WARMUP_TIMEOUT_S}s"
        + ("; priming the cache with the real prompt prefix" if prime is prime_messages
           else "; minimal probe -- no prefix to prime") + ")...")
    start = datetime.now(timezone.utc)
    try:
        call_ollama(host, model, prime, temperature, num_ctx,
                    timeout=WARMUP_TIMEOUT_S, tools=False, top_p=top_p, top_k=top_k,
                    max_tokens=1)
    except Exception as e:
        # A warmup that fails on the REAL prefix but would have succeeded on
        # "ready" must not take the dispatch down with it -- the point of this
        # change is cache priming, and priming is an optimisation. Fall back
        # once, loudly, so the run still happens and the cause is on the record.
        if prime is prime_messages:
            log(f"[worker] warmup with the real prefix failed ({type(e).__name__}: {e}); "
                f"falling back to the minimal probe. The run continues; turn 1 will "
                f"simply pay its own prefill as it did before this optimisation.")
            call_ollama(host, model, [{"role": "user", "content": "ready"}], temperature,
                        num_ctx, timeout=WARMUP_TIMEOUT_S, tools=False,
                        top_p=top_p, top_k=top_k)
        else:
            raise
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log(f"[worker] {model} loaded and warm ({elapsed:.0f}s).")
    if any(h in host for h in _unraid_host_substrings()):
        # Fable's suggestion, 2026-08-29 (Unraid CUDA-OOM investigation): the one real
        # VRAM signal available without SSH/nvidia-smi access to that box -- /api/ps
        # reports size_vram per resident model. Logging it here on every Unraid
        # dispatch means a future OOM investigation has real numbers on file instead
        # of having to guess at "what else might have been using VRAM" after the fact
        # (which is exactly the position this session was in tonight).
        try:
            req = urllib.request.Request(f"{host}/api/ps")
            with urllib.request.urlopen(req, timeout=15) as resp:
                ps = json.loads(resp.read())
            for m in ps.get("models", []):
                gb = (m.get("size_vram") or 0) / 1e9
                log(f"[worker] Unraid VRAM check: {m.get('name')} using {gb:.2f}GB")
        except Exception as e:
            log(f"[worker] Unraid VRAM check failed (non-fatal): {e}")

    try:
        req = urllib.request.Request(f"{host}/api/ps")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ps = json.loads(resp.read())
        target = _normalize_model_name(model)
        entry = next((m for m in ps.get("models", []) if _normalize_model_name(m.get("name", "")) == target), None)
        if entry:
            total = entry.get("size") or 0
            vram = entry.get("size_vram") or 0
            if total > 0 and vram < total:
                spilled_frac = (total - vram) / total
                exception = UNRAID_SPILLOVER_EXCEPTIONS.get(_normalize_model_name(model)) or \
                    UNRAID_SPILLOVER_EXCEPTIONS.get(model)
                if exception is not None and spilled_frac <= exception["max_spill_frac"]:
                    log(f"[worker] {model} on {host} loaded with {(total - vram)/1e9:.2f}GB "
                        f"({spilled_frac*100:.1f}%) off-GPU -- within its documented "
                        f"UNRAID_SPILLOVER_EXCEPTIONS allowance ({exception['max_spill_frac']*100:.0f}%), "
                        f"proceeding (MoE expert-offload, not dense spillover).")
                else:
                    raise RuntimeError(
                        f"{model} on {host} loaded with {(total - vram)/1e9:.2f}GB "
                        f"({spilled_frac*100:.1f}%) off-GPU at --num-ctx {num_ctx} "
                        f"(total {total/1e9:.2f}GB, VRAM {vram/1e9:.2f}GB) -- ANY spillover aborts "
                        f"unless the model has a documented UNRAID_SPILLOVER_EXCEPTIONS entry it fits "
                        f"under (none does here). Aborting before spending real dispatch time on a "
                        f"degraded host. Lower --num-ctx, or use pick_host()'s auto-selection (omit --host)."
                    )
    except RuntimeError:
        raise
    except Exception as e:
            log(f"[worker] WARNING: spillover check against {host}/api/ps failed (non-fatal, proceeding): {e}")


# Process exit codes (main() does sys.exit(run_task(...))): 0 = converged,
# 1 = verify failed, 2 = did not converge (genuinely ran out of budget, and
# verify either wasn't given or didn't pass -- nothing here is confirmed good).
# 3 = paused for review -- the run stopped cleanly with a resumable transcript
# on disk and is NOT a failure. Covers BOTH pause sources: the model's own
# request_more_iterations / context-threshold review gates AND an external
# SIGTERM from the queue daemon's promote flow (see _sigterm_pause_handler).
# ollama-queue.py reads this constant off the imported worker module so the
# two can't drift apart. 4 = refused to start (dispatched outside the queue,
# see the OLLAMA_DISPATCH_VIA_QUEUE guard in main()). 5 = EXIT_CODE_DONE_UNCONVERGED
# (below) -- the mirror image of the vacuous-pass guard: did not converge, but
# verify PASSED, so real completed work exists despite the loop not exiting
# tidily. Distinct from plain 2 so a reviewer triaging by status doesn't
# discard a genuinely finished, verified change just because the model kept
# talking after finishing (github-projects-bf caught this live 2026-08-29:
# job nfc-check-section hit DID-NOT-CONVERGE after 30 iterations, but had
# already made 3 real edits and passed its own --verify; reported FAILED,
# nearly got discarded unread).
EXIT_CODE_PAUSED = 3
EXIT_CODE_DONE_UNCONVERGED = 5

# Pause reasons that represent a DELIBERATE grant of more budget: a human (or
# the queue's auto-resume) looked at the pause and chose to hand the job more
# room. Resuming from one of these extends the ceiling by a fresh max_iters.
# Everything else -- above all `external_sigterm`, which is the queue daemon
# preempting or kickstarting, not a decision about this job -- granted nothing.
BUDGET_GRANTING_PAUSE_REASONS = frozenset({
    "request_more_iterations",
    "context_threshold",
})


FIXED_LANE_TERMINAL_REASONS = ("fixed_lane_exhausted", "fixed_lane_apply_failed", "fixed_lane_transport_error")
_FIXED_LANE_NONE = {"converged": False, "summary": None, "files_changed": 0, "reason": None, "status": None}


def fixed_lane_handoff_text(handoff) -> str:
    """PURE. The block appended to the FIRST user message when the fixed lane failed, so the open
    loop starts from the lane's best attempt instead of from nothing."""
    h = handoff or {}
    parts = ["\n\n---\nHARNESS NOTE: a fixed one-shot repair lane already tried this task and did not "
             f"get the verify green (reason: {h.get('reason') or 'unknown'})."]
    if h.get("seed_applied"):
        parts.append("Its best attempt is ALREADY APPLIED in the working tree (diff below). Build on it, "
                     "or revert parts of it if it is wrong; do not start over blindly.")
    elif h.get("seed_diff"):
        parts.append("Its best attempt could not be applied automatically; the diff is below for reference.")
    if h.get("seed_diff"):
        parts.append("Seed diff:\n```diff\n" + str(h["seed_diff"])[:6000] + "\n```")
    if h.get("last_verify_output"):
        parts.append("Last verify output (what is still red):\n```\n" + str(h["last_verify_output"])[-3000:] + "\n```")
    return "\n".join(parts)


def _apply_fixed_lane_seed(cwd, handoff) -> bool:
    """Apply the lane's seed diff (git apply, else write the seed files) so the open loop continues
    from it. Never raises; False when nothing was applied."""
    try:
        diff = (handoff or {}).get("seed_diff") or ""
        if not diff.strip():
            return False
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=str(cwd), input=diff,
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True
        n = 0
        for rel, text in ((handoff or {}).get("seed_files") or {}).items():
            if not isinstance(text, str):
                continue
            fp = resolve_path(Path(cwd), rel)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text)
            _drop_stale_pyc(fp)
            n += 1
        return n > 0
    except Exception:
        return False


def _run_fixed_lane_seam(*, mode, model, host, cwd, task, verify, task_kind, api_style, resume_from,
                         bash_only, manual_tools, targets, rb, messages, lane_module=None):
    """--mode auto|fixed: try the one-shot repair lane (fixed_lane.py) before the open loop.
    -> dict(converged, summary, files_changed, reason, status). converged=True on passed /
    already_green (the caller then runs the normal verify + gate path). Any other lane outcome
    leaves the worktree as the lane's best attempt (seed) and INJECTS result['handoff'] (seed diff +
    last verify output) into messages[1], then returns converged=False so the open loop runs.
    reason is one of FIXED_LANE_TERMINAL_REASONS when the lane ran and failed, else None."""
    out = dict(_FIXED_LANE_NONE)
    skip = None
    if resume_from:
        skip = "resumed run"
    elif task_kind != "coding":
        skip = "not a coding task"
    elif not (verify or "").strip():
        skip = "no verify command"
    elif api_style != "openai":
        skip = f"lane needs the OpenAI-style lane (api={api_style})"
    elif bash_only or manual_tools:
        skip = "bash-only/manual-tools action mode"
    if skip:
        log(f"[worker] FIXED LANE ({mode}): skipped -- {skip}; open loop.")
        return out
    try:
        fl = lane_module
        if fl is None:
            import fixed_lane as fl
        ltask = {"cwd": str(cwd), "targets": list(targets or []), "verify": verify,
                 "spec": task, "title": (task or "").strip().splitlines()[0][:200] if (task or "").strip() else ""}
        ok, why = fl.lane_eligible(ltask, model=model)
        if not ok:
            log(f"[worker] FIXED LANE ({mode}): ineligible -- {'; '.join(why)}; open loop.")
            return out
        guard_fn = None
        if not (_SYNTAX_GUARD_ON[0] and _wr.syntax_guard_enabled(rb)):
            guard_fn = lambda path, before, after: (True, [])   # noqa: E731  (profile/env kill switch)
        log(f"[worker] FIXED LANE ({mode}): running on {len(ltask['targets'])} target file(s).")
        res = fl.run_lane(ltask, model=model, host=host, log=log, guard_fn=guard_fn)
    except Exception as e:
        log(f"[worker] FIXED LANE ({mode}): crashed ({type(e).__name__}: {str(e)[:200]}) -- open loop.")
        _dispatch_metrics["fixed_lane"] = {"status": "crashed", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        return out
    status = res.get("status")
    out["status"] = status
    _dispatch_metrics["fixed_lane"] = {
        "status": status, "reason": res.get("reason"), "tokens": res.get("tokens"),
        "wall_s": res.get("wall_s"), "mode": mode}
    _dispatch_metrics["sum_completion_tokens"] = (_dispatch_metrics.get("sum_completion_tokens") or 0) + int(res.get("tokens") or 0)
    log(f"[worker] FIXED LANE RESULT: status={status} reason={res.get('reason')} tokens={res.get('tokens')} "
        f"wall_s={res.get('wall_s')}")
    if status in ("passed", "already_green"):
        out["converged"] = True
        out["summary"] = (f"fixed lane: {status} (verify green)"
                          + (f", {res.get('tokens')} tokens, {res.get('wall_s')}s" if status == "passed" else ""))
        try:
            out["files_changed"] = len((res.get("best") or {}).get("files") or {}) if status == "passed" else 0
        except Exception:
            out["files_changed"] = 1 if status == "passed" else 0
        return out
    if status == "ineligible":
        return out
    reason = res.get("reason")
    if reason in FIXED_LANE_TERMINAL_REASONS or (reason or "").startswith("fixed_lane_"):
        out["reason"] = reason
    handoff = dict(res.get("handoff") or {})
    handoff.setdefault("reason", reason)
    handoff["seed_applied"] = _apply_fixed_lane_seed(cwd, handoff)
    try:
        messages[1]["content"] = str(messages[1].get("content") or "") + fixed_lane_handoff_text(handoff)
        log(f"[worker] FIXED LANE handoff injected into the first user message "
            f"(seed applied={handoff['seed_applied']}); falling into the open loop.")
    except Exception as e:
        log(f"[worker] FIXED LANE handoff injection failed ({e!r}); open loop without it.")
    return out


EXTRA_ITERATIONS_MAX = 200
TURN_LOG_MAX = 120


def turn_log_record(i, resp, msg, thinking, usage, elapsed, turn_think) -> dict:
    """PURE. One compact per-turn row for dispatch-metrics (`turn_log`): thinking chars (the
    separate reasoning field PLUS Darkbloom's inline thinking that arrives inside content before
    the first </think>), content chars, finish_reason, token counts, requested thinking mode and
    (filled in by the loop) tool-call parse outcome. Needed for the thinking/sampling A/B."""
    content = str((msg or {}).get("content") or "")
    inline = content.split("</think>", 1)[0] if "</think>" in content else ""
    usage = usage or {}
    _rec = {
        "i": i,
        "thinking_chars": len(thinking or "") + len(inline),
        "content_chars": len(content),
        "tool_calls": len((msg or {}).get("tool_calls") or []),
        "parse": None,
        "finish_reason": (resp or {}).get("done_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "elapsed_s": round(float(elapsed or 0), 1),
        "think_requested": turn_think,
    }
    if (resp or {}).get("_reasoning_parser"):      # REASONING_PARSER lever on only: default rows keep their old shape
        _rec["reasoning_parser"] = True
        _rec["reasoning_content_chars"] = len(thinking or "")
        if (resp or {}).get("_unclosed_think"):
            _rec["unclosed_think"] = True
    return _rec


def apply_extra_iterations(total_iters: int, extra) -> tuple:
    """PURE. -> (new_total, applied). `extra` (--extra-iterations) raises the ceiling by that many
    iterations; None/negative/garbage = 0, capped at EXTRA_ITERATIONS_MAX so a bad caller cannot
    buy an unbounded run."""
    try:
        n = int(extra or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, EXTRA_ITERATIONS_MAX))
    return total_iters + n, n


def resume_total_iters(resumed_at_iteration: int, max_iters: int,
                       pause_reason: "str | None") -> int:
    """The iteration ceiling for this session.

    WHY (bg-crypto s3, 2026-09-19): this was unconditionally
    `resumed_at_iteration + max_iters`, so EVERY resume bought a full fresh
    budget on top of the iterations already burned. `max_iters` was therefore a
    per-resume allowance, not a bound on the job. Job c71f0581d998 was
    externally SIGTERM'd and resumed six times and its ceiling walked
    14 -> 21 -> 24 -> 26 -> 27, running 09:24 to 15:29 -- about six hours -- on
    a nominal 14-iteration budget that never once bound it.

    That compounds badly with the daemon's kickstart-orphans bug: every
    involuntary SIGTERM produces a resume, and every resume silently bought
    another full budget, so the more the queue churned the less max_iters meant.

    A deliberate grant still extends -- that is the documented review-gate flow
    (`request_more_iterations` pauses, a human resumes with more budget), and
    `context_threshold` likewise resumes with a raised ceiling. An involuntary
    pause restores the ORIGINAL ceiling instead: the work resumes where it left
    off, with the budget it always had.

    A fresh run passes resumed_at_iteration=0 and is unaffected either way.
    """
    if pause_reason is None or pause_reason in BUDGET_GRANTING_PAUSE_REASONS:
        return resumed_at_iteration + max_iters
    # Involuntary pause: no new budget. Never hand back a ceiling below the
    # iterations already completed -- that would make range() empty and the
    # resume a silent no-op, trading a runaway budget for a dead job.
    return max(max_iters, resumed_at_iteration)


# Set by the SIGTERM handler installed at the top of run_task: the queue
# daemon's promote flow (drop a pending job onto a running one in the
# dashboard) sends SIGTERM to pause a running dispatch gracefully instead of
# killing it -- finish the current iteration, save the transcript, exit with
# EXIT_CODE_PAUSED. Checked once per iteration, right after that iteration's
# incremental transcript save (see run_task's loop), so the transcript on
# disk is always complete through some whole iteration and resuming loses
# nothing.
_sigterm_pause_requested = False


def _sigterm_pause_handler(signum, frame):
    global _sigterm_pause_requested
    _sigterm_pause_requested = True
    log("[worker] SIGTERM received -- finishing the current iteration, then pausing "
        "gracefully (transcript saved, exit code 3; resume with --resume <transcript>).")


def _changed_file_count(cwd) -> int | None:
    """How many paths did this dispatch leave changed? None if unknowable.

    Recorded on every metrics row so a later question like "do multi-deliverable
    jobs cap out" has a size measure on the OUTPUT side, not just task_chars on
    the input side -- task_chars turned out to be a near-useless predictor
    (r=0.031 against iterations over 309 rows), and the number of files a job
    actually had to touch is the more plausible candidate.

    Union of tracked changes and untracked files, because a job whose whole
    output is new files would otherwise count as zero. Never raises: this runs
    on the metrics path, and metrics must never be able to fail a dispatch.
    """
    try:
        names = subprocess.run(["git", "diff-index", "--name-only", "HEAD", "--"], cwd=cwd,
                                capture_output=True, text=True, timeout=15)
        if names.returncode != 0:
            return None
        paths = {l for l in names.stdout.splitlines() if l.strip()}
        unt = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                              cwd=cwd, capture_output=True, text=True, timeout=15)
        if unt.returncode == 0:
            paths |= {l for l in unt.stdout.splitlines() if l.strip()}
        return len(paths)
    except Exception:
        return None


SALVAGE_MAX_BYTES = 2_000_000


def save_salvage_patch(cwd, dest) -> dict:
    """SALVAGE ON EXIT (SWE-agent autosubmit-on-error, 2026-10-09). What the worker already did on a
    loop_detected / stop_gate_failed / cap exit: left the worktree untouched, ran the end-of-run
    verify, and (verify green) exited DONE-BUT-UNCONVERGED. What it did NOT do: keep a copy of the
    diff outside the worktree, so a reaped/reset worktree discarded a near-complete patch. This
    writes tracked changes (git diff-index -p HEAD, plumbing: never rewrites the index) plus
    untracked files (as new-file diffs) to `dest`. Deterministic, bounded (2MB), never raises.
    -> {"path", "bytes", "files"} or {} when there is nothing to save / not a git tree."""
    try:
        cwd = str(cwd)
        if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, capture_output=True,
                          text=True, timeout=10).stdout.strip() != "true":
            return {}
        parts = []
        tracked = subprocess.run(["git", "diff-index", "-p", "HEAD", "--"], cwd=cwd, capture_output=True,
                                 text=True, timeout=30)
        if tracked.returncode == 0 and tracked.stdout:
            parts.append(tracked.stdout)
        unt = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
                             capture_output=True, text=True, timeout=15)
        for f in [l for l in unt.stdout.splitlines() if l.strip()][:200]:
            d = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", f], cwd=cwd,
                               capture_output=True, text=True, timeout=15)
            if d.stdout:
                parts.append(d.stdout)
        body = "".join(parts)
        if not body.strip():
            return {}
        body = body[:SALVAGE_MAX_BYTES]
        Path(dest).write_text(body)
        files = len(re.findall(r"^diff --git ", body, re.M))
        return {"path": str(dest), "bytes": len(body), "files": files}
    except Exception:
        return {}


def _git_worktree_snapshot(cwd) -> str:
    """Ground-truth "did anything in this working tree actually change" check for the
    vacuous-verify-pass guard (Fable ruling 2026-08-29, following a real miss
    github-projects-bf caught: a session that only explored via run_bash -- no edits,
    no diff -- still only WARNED under the first version of this guard, which inferred
    intent from tool-call counts instead of checking the tree itself). Returns None if
    cwd is not inside a git work tree at all (the guard falls back to the tool-call
    heuristic in that case); otherwise a string combining HEAD's rev, `git status
    --porcelain`, and `git diff HEAD`, taken together specifically because any one
    alone has a real gap the others cover:
      - porcelain alone: a file already modified BEFORE the dispatch started shows the
        identical status line even if the model edits it further -- false vacuous-fail
        on real work if compared start-vs-end with porcelain only.
      - status+diff alone (no rev): a model that COMMITS via run_bash leaves porcelain
        and diff-from-HEAD both clean even though the tree genuinely changed -- a moved
        HEAD is what proves that happened.
    Known residual gap, accepted rather than solved: editing an already-untracked file
    that was present before the dispatch started (its `??` line in porcelain doesn't
    change, and `git diff HEAD` doesn't see untracked content at all) is invisible to
    this check. Not worth a second, heavier mechanism (content hashing an unbounded
    tree) for what's a false-negative on an already-narrow guard, not a false-positive
    that would fail real work."""
    try:
        is_repo = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                  capture_output=True, text=True, timeout=10)
        if is_repo.returncode != 0 or is_repo.stdout.strip() != "true":
            return None
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                              capture_output=True, text=True, timeout=10)
        rev_str = rev.stdout.strip() if rev.returncode == 0 else "NO_COMMITS_YET"
        status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                                 capture_output=True, text=True, timeout=15)
        diff_str = ""
        if rev.returncode == 0:
            diff = subprocess.run(["git", "diff-index", "-p", "HEAD", "--"], cwd=cwd,  # plumbing: never rewrites .git/index (GIT_OPTIONAL_LOCKS note)
                                   capture_output=True, text=True, timeout=30)
            diff_str = diff.stdout if diff.returncode == 0 else ""
        return f"{rev_str}\n---STATUS---\n{status.stdout}\n---DIFF---\n{diff_str}"
    except Exception:
        return None


def _save_transcript(log_path: Path, model: str, host: str, cwd: Path, task: str,
                      converged: bool, iteration: int, messages: list,
                      pause_reason: str = None, pause_meta: dict = None,
                      worktree_start_snapshot: str = None,
                      baseline_verify_sig=None) -> None:
    """Write the current transcript state to log_path -- shared by the
    incremental per-iteration save and the final end-of-run save (see
    run_task's --resume support for why incremental saving exists). Same
    JSON shape either way, so a partially-written (paused) transcript and a
    finished one are both valid --resume input.

    pause_reason/pause_meta (added 2026-08-29, paired with ollama-queue.py's
    auto-resume watchdog): a machine-readable classification of WHY a paused
    run stopped, distinct from the free-text log line meant for a human. One
    of "context_threshold", "request_more_iterations", "external_sigterm",
    or None (not paused / converged / failed normally). Reading this back on
    resume lets the queue daemon decide whether it's even safe to auto-bump
    and retry (context/iteration shortfalls are), vs. an external_sigterm
    pause, which was someone's deliberate stop and must never be
    auto-resumed.

    worktree_start_snapshot (added 2026-08-29, Fable ruling on the vacuous-
    pass guard's git-diff discriminator): the working tree's state (see
    _git_worktree_snapshot) as of the ORIGINAL dispatch's start, persisted so
    a --resume'd session compares against the true start, not a fresh
    snapshot taken after the pause -- a fresh snapshot at resume time would
    wrongly read real pre-pause edits + post-resume inaction as "nothing
    changed"."""
    payload = json.dumps({
        "model": model, "host": host, "cwd": str(cwd), "task": task,
        "converged": converged, "iterations": iteration, "messages": messages,
        "worktree_start_snapshot": worktree_start_snapshot,
        # Sets aren't JSON-serializable; store sorted for a stable, diff-friendly transcript.
        # Restored to a set on --resume (see run_task). None stays None = "no baseline".
        "baseline_verify_sig": (sorted(baseline_verify_sig)
                                 if baseline_verify_sig is not None else None),
        "pause_reason": pause_reason, "pause_meta": pause_meta or {},
    }, indent=2)
    # Atomic: the daemon's SIGTERM->SIGKILL preemption can land mid-write of a
    # multi-hundred-KB transcript, and a truncated file at the exact path the
    # CHECKPOINT TRANSCRIPT marker names means the resume fails to parse and
    # the job restarts from scratch. Same bug class as the queue-state
    # quarantine (2026-09-21): never leave a half-written JSON where a reader
    # expects a whole one.
    tmp = log_path.with_name(log_path.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, log_path)


def _git_changed_paths(cwd):
    """Paths changed in the worktree vs HEAD (tracked modifications + untracked, minus ignored)."""
    out = []
    for cmd in (["git", "diff", "--name-only", "HEAD"], ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        if r.returncode == 0:
            out.extend(l.strip() for l in r.stdout.splitlines() if l.strip())
    return out


def _quick_verify(verify: str, cwd) -> tuple:
    """Lightweight in-loop verify check, used only to gate an early completion claim
    (the task_complete tool, or a silent no-tool-call final answer) before accepting
    it. Fable design 2026-08-29, built against real transcripts where a model
    declared itself done on objectively incomplete work and the harness had no way
    to notice until the run had already ended.

    Deliberately NOT the authoritative check -- the existing end-of-run verify
    block (with its vacuous-pass worktree-diff guard) still runs exactly once after
    the loop actually ends, regardless of how it ended, and its result is what
    decides the real exit code. This just answers "does verify pass right now,"
    cheaply enough to call a bounded few times per dispatch. Bounded by each call
    site's own claim/nudge cap, so worst case this doubles the verify command's
    cost for a dispatch that needed the retries -- acceptable next to burning the
    rest of the iteration budget on a task the model already believes is done.
    """
    _qv_t0 = time.monotonic()
    try:
        rc, out, err = _run_shell_group(verify, cwd, 300)
    except _GroupTimeout:
        if _wl is not None:
            _wl.note_verify_secs(time.monotonic() - _qv_t0)
        return False, "(verify command timed out after 300s)"
    if _wl is not None:
        _wl.note_verify_secs(time.monotonic() - _qv_t0)
    if rc in (126, 127):
        return False, (f"(verify command itself could not run, exit {rc} -- "
                        f"check the --verify string)\n{err}")
    output = (out or "") + (("\n" + err) if err else "")
    if rc == 0:
        _edit_checkpoint_on_pass(cwd, output)
    return rc == 0, output


# --- EDIT CHECKPOINT (EDIT_CHECKPOINT=1, default OFF; 2026-10-09) ---------------------------------
# Evidence: Coherence Collapse (arXiv 2603.24631) -- agents often reach a correct patch mid-run and
# then overwrite it; an iteration cap alone does not prevent that. Convergence in this worker is
# declared only at a task_complete claim / silent final answer whose in-loop verify (_quick_verify)
# passes, so a passing verify that does NOT end the run happens when the stop-gate refuses the claim
# (required files / must-contain missing), the verify-flipped pause fires, or a nudge continues the
# loop. This hook sees EVERY passing in-loop verify: when the required files are present it snapshots
# the working-tree diff (save_salvage_patch: tracked + untracked, outside the worktree) next to the
# transcript. At the end of the run, if the run did NOT converge and the end-of-run verify did not pass,
# the snapshot is REPORTED (log line + dispatch-metrics `edit_checkpoint`), never applied: restoring is a
# `git apply` of the named file, so the tree is never mutated behind the operator's back.
_EDIT_CKPT = {"cwd": None, "dest": None, "files": (), "lits": (), "info": None, "passes": 0,
              "verify": None}


def _edit_checkpoint_on_bash_pass(cwd, command, output="") -> dict:
    """The model ran a run_bash command that exits 0 and CONTAINS the task's verify command: the
    harness only runs the verify at a completion claim, so this is where a mid-run pass is visible
    (the Coherence-Collapse case: a correct patch exists, then the model keeps editing)."""
    try:
        v = " ".join(str(_EDIT_CKPT.get("verify") or "").split())
        if not v or not _edit_checkpoint_enabled():
            return {}
        if v not in " ".join(str(command or "").split()):
            return {}
        return _edit_checkpoint_on_pass(cwd, output)
    except Exception:
        return {}


def _edit_checkpoint_enabled() -> bool:
    return os.environ.get("EDIT_CHECKPOINT") == "1"


def _edit_checkpoint_on_pass(cwd, verify_output="") -> dict:
    """Called on every passing in-loop verify. -> the snapshot info dict or {} (disabled / not armed /
    required files missing / empty diff). Never raises."""
    try:
        c = _EDIT_CKPT
        if not _edit_checkpoint_enabled() or not c.get("dest"):
            return {}
        if str(cwd) != str(c.get("cwd")):
            return {}
        c["passes"] += 1
        try:
            changed = _git_changed_paths(cwd)
        except Exception:
            changed = []
        ok, fails = _wr.check_stop_criteria(cwd, c["files"], c["lits"], verify_ok=None,
                                            extra_files=changed)
        if not ok:
            log(f"[worker] EDIT-CHECKPOINT: verify passed but required criteria are unmet "
                f"({fails}) -- no snapshot taken.")
            return {}
        info = save_salvage_patch(cwd, c["dest"])
        if not info:
            return {}      # empty diff: a pass over an unchanged tree is not a patch worth keeping
        info = dict(info, passes=c["passes"],
                    verify_tail=(verify_output or "")[-300:])
        c["info"] = info
        log(f"[worker] EDIT-CHECKPOINT: verify PASSED with required criteria met -- snapshot "
            f"{info['files']} file(s), {info['bytes']} bytes at {info['path']} (pass #{c['passes']}).")
        return info
    except Exception as e:
        log(f"[worker] EDIT-CHECKPOINT error (ignored): {e!r}")
        return {}


# Diagnostic-line shapes for _verify_failure_signature. Kept module-level (compiled
# once) and deliberately SHAPE-ANCHORED: each requires a source location or an explicit
# error/failure token, so aggregate/summary/timing lines ("Found 168 errors", "117
# tests / 3 failed", "duration_ms 101") never match. See the helper's docstring for why
# that asymmetry is the safe one.
_VERIFY_DIAG_RES = (
    re.compile(r'\(\d+,\s*\d+\):\s*(error|warning)\b', re.I),   # tsc:   path(line,col): error TSxxxx
    re.compile(r':\d+:(\d+:)?\s*(error|warning)\b', re.I),      # gcc/eslint: path:line[:col]: error ...
    re.compile(r'\b(error|warning)\b.*\bTS\d+\b', re.I),        # TS diagnostic code anywhere on the line
    re.compile(r'^(FAIL|✕|✗|×|not ok|AssertionError|Traceback)\b'),  # common test-runner failure markers
    # Additions per Fable review 2026-08-30 -- the set above missed several real
    # failure shapes, which (combined with the empty-signature fix below) would have
    # let an unrecognized NEW failure read as "no new failures" and false-accept:
    re.compile(r'^FAILED\b'),                        # pytest: FAILED tests/x.py::y  (\bFAIL rejects FAILED)
    re.compile(r'^--- FAIL\b'),                      # go test: --- FAIL: TestX
    re.compile(r'error\[E\d+\]'),                    # rust: error[E0308]: mismatched types
    re.compile(r'^\S+\.\w{1,4}:\d+:\d+:'),           # go/generic compile: path.go:12:5: undefined: foo
)


def _verify_failure_signature(output: str) -> set:
    """Normalize a --verify command's OUTPUT into a set of per-defect 'signature' lines,
    so two runs can be diffed to isolate the failures a model's diff actually INTRODUCED
    from failures already present at dispatch start (the baseline).

    Added 2026-08-30 after a real convergence failure (resell-tracker): a --verify of
    `npx tsc --noEmit && npm test` returned 168 errors on the UNEDITED worktree -- a
    missing `prisma generate` left every DB result typed `any`. The dispatched model made
    its two CORRECT edits, which left all 168 pre-existing errors in place, and the harness
    fed the whole 168-line dump back as 'fix the reported problems': undirectable noise in
    files the model never opened. It correctly refused to touch them and looped
    task_complete to the cap, then reported FAILED. Diffing current-vs-baseline turns that
    into 'you introduced 0 new failures', which is directable (accept) instead of noise.

    Normalization keeps only DIAGNOSTIC-looking lines (individual compiler errors and
    test-failure markers, per _VERIFY_DIAG_RES) and drops aggregate/summary/timing lines.
    Rationale for that specific asymmetry: a single diagnostic is deterministic and
    location-anchored, so set-difference isolates exactly the diagnostics attributable to
    the diff; aggregate lines are per-RUN not per-defect and drift with counts (168 vs
    165), so including them would flag a pure REDUCTION in errors as a spurious 'new' line.
    A line matching no known diagnostic shape is DROPPED rather than guessed at -- the
    failure modes are not symmetric: a false 'no new failures' is still caught downstream
    by the end-of-run authoritative verify (which continues to fail on real regressions
    whose lines we happened not to recognize), whereas a false 'new failure' would
    re-introduce exactly the undirectable-noise problem this helper exists to remove."""
    sig = set()
    for raw in (output or "").splitlines():
        line = raw.strip()
        if line and any(r.search(line) for r in _VERIFY_DIAG_RES):
            sig.add(line)
    return sig


def _verify_delta_feedback(verify: str, cwd, baseline_sig):
    """Run the verify once and classify the result against the persisted baseline
    signature. Returns (passed, new_failures, preexisting_present, current_recognized, raw_output):
      passed              -- verify exited 0 (no failures at all)
      new_failures        -- sorted list of diagnostic lines present now but NOT at
                             baseline (i.e. attributable to the model's diff)
      preexisting_present -- True if any current failure was already in the baseline
      current_recognized  -- True if the CURRENT failing output produced at least one
                             recognized diagnostic line. Critical for the no-regression
                             decision (Fable review 2026-08-30): a failing verify whose
                             lines match no regex yields an EMPTY current signature, which
                             must NOT be read as 'no new failures' -- callers require this
                             True before accepting a BASELINE-BROKEN no-regression run, and
                             otherwise fall back to raw-output feedback.
      raw_output          -- the verify command's combined stdout+stderr

    When baseline_sig is None (no baseline captured -- a verify that couldn't run at start,
    a legacy resumed transcript, or a non-coding task) every current failure counts as 'new'
    and current_recognized reflects only what we could parse; callers treat None-baseline as
    'cannot classify' and fall back to the old raw-output behavior."""
    ok, out = _quick_verify(verify, cwd)
    if ok:
        return True, [], False, False, out
    current = _verify_failure_signature(out)
    if baseline_sig is None:
        return False, sorted(current), False, bool(current), out
    new = current - baseline_sig
    preexisting = bool(current & baseline_sig)
    return False, sorted(new), preexisting, bool(current), out


# --- Diff-attributed verify feedback + stop-early guard (2026-10-02) -------------
# Live: f21621ab528f (sidecar-bfmr-sink-s1-route). The diff introduced TS18047 at
# route.ts:287; the feedback the model got was the RAW verify output under a note
# saying the verify "was ALREADY FAILING before you started ... pre-existing
# failures", so it concluded the new error was "a pre-existing error in the original
# code" and stopped at 12/30 (two silent-stop nudges spent, then accepted as a final
# answer). Three rules now, each a pure helper the loop calls:
#   * the model is shown ONLY the diagnostics its diff introduced, as file:line:
#     message, stated as caused by its own edits -- no baseline text next to them;
#   * a run may not END (silent stop, or a task_complete past the claim cap) while
#     the verify reports failures this diff introduced -- only max_iters ends it;
#   * a turn cut off at the output cap is not a "final answer" (f21621 iterations
#     10-12 each generated exactly 8192 tokens of prose and no tool call).
_DIAG_LOC_RES = (
    re.compile(r"^(?P<f>[^\s(:]+)\((?P<l>\d+),(?P<c>\d+)\):\s*(?P<m>.+)$"),     # tsc
    re.compile(r"^(?P<f>[^\s:]+):(?P<l>\d+):(?:(?P<c>\d+):)?\s*(?P<m>.+)$"),      # gcc/eslint/py
)


def format_new_diagnostic(line: str) -> str:
    """PURE. One diagnostic as `file:line: message` (column dropped from the anchor,
    kept in nothing -- the line is what the model needs to open)."""
    t = (line or "").strip()
    for r in _DIAG_LOC_RES:
        m = r.match(t)
        if m:
            return "%s:%s: %s" % (m.group("f"), m.group("l"), m.group("m").strip())
    return t


def attributable_new_failures(new_failures, baseline_sig) -> list:
    """PURE. Failures provably introduced by this diff: only when a baseline was
    captured (a passing baseline is the empty set, so every failure is new)."""
    if baseline_sig is None:
        return []
    return list(new_failures or [])


def new_failure_feedback(new_failures, verify_out="", baseline_is_the_task=False) -> str:
    """PURE. The model-facing text for a still-failing verify with failures THIS
    diff introduced. Never mentions pre-existing/baseline failures next to them."""
    seen, items = set(), []
    for l in new_failures or []:
        f = format_new_diagnostic(l)
        if f not in seen:
            seen.add(f)
            items.append("  - " + f)
    body = ("Your changes INTRODUCED these %d verification failure(s). They did NOT exist "
            "before your edits -- your diff caused them, so you must fix them:\n%s"
            % (len(items), "\n".join(items)[-3000:]))
    if baseline_is_the_task:
        body += ("\nFix those first. The task is done only when the verify command PASSES "
                 "as a whole; the full current output is:\n--- verify output ---\n%s\n--- end ---"
                 % (verify_out or "")[-2000:])
    return body


def silent_stop_decision(v_ok, attributable_new, nudges, max_nudges,
                         baseline_is_the_task, no_regression, did_work) -> str:
    """PURE. What a no-tool-call turn means for a coding task with a verify:
    'accept' (stop) or 'nudge' (feed back and keep going). A verify that reports
    failures this diff introduced is NEVER accepted -- the nudge cap does not apply
    to it; only max_iters ends that run."""
    if v_ok:
        return "accept"
    if attributable_new:
        return "nudge"
    if nudges >= max_nudges:
        return "accept"
    if baseline_is_the_task or not (no_regression and did_work):
        return "nudge"
    return "accept"


def output_cap_cut_prose(msg, usage, done_reason, max_tokens) -> bool:
    """PURE. This turn wrote prose, made no tool call, and stopped because the output
    cap ran out -- it was cut off mid-turn, it did not choose to stop."""
    if msg.get("tool_calls") or not (msg.get("content") or "").strip():
        return False
    if str(done_reason or "").lower() == "length":
        return True
    ct = (usage or {}).get("completion_tokens") or 0
    return bool(max_tokens) and ct >= max_tokens


def compact_cut_off_prose(content, keep_chars=None):
    """PURE. (compacted_text, dropped_chars) for a turn CUT OFF at the output cap.
    The cut turn stays in the transcript the model re-reads; kept verbatim (~38KB of
    a 12-22-unique-line cycle, live 984db5b6a535/06c2bf413cd7/62dcd980447d) it primes
    the next turn to reproduce the same cycle -- 62dcd980447d's turns 56/58/60 were
    byte-identical 31840-char copies. Keep the first occurrence of each distinct
    non-blank line, cap at keep_chars, and label what was dropped."""
    keep = OUTPUT_CAP_CUT_KEEP_CHARS if keep_chars is None else keep_chars
    text = content or ""
    seen, out, size = set(), [], 0
    for line in text.split("\n"):
        key = line.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        if size + len(line) + 1 > keep:
            break
        out.append(line)
        size += len(line) + 1
    kept = "\n".join(out).rstrip()
    dropped = len(text) - len(kept)
    if dropped <= 0:
        return text, 0
    return (kept + f"\n\n[worker: the rest of this turn ({dropped} chars, repeated/cut-off "
                   f"analysis) was dropped -- it hit the output cap without acting]"), dropped


def degenerate_repetition(text):
    """PURE. Reason string if a tool-call-free turn has degenerated into repetition, else None:
    the same >= PROSE_LINE_REPEAT_MIN_LEN-char line PROSE_LINE_REPEAT_MAX+ times, or the
    last PROSE_BLOCK_CHARS chars occurring PROSE_BLOCK_REPEATS+ times in the turn. Short
    structural lines ("}", "],") are exempt: they legitimately repeat in code."""
    if not text:
        return None
    counts = {}
    for ln in text.split("\n"):
        k = ln.strip()
        if len(k) >= PROSE_LINE_REPEAT_MIN_LEN:
            n = counts.get(k, 0) + 1
            if n >= PROSE_LINE_REPEAT_MAX:
                return f"line repeated x{n}: {k[:60]!r}"
            counts[k] = n
    if len(text) >= PROSE_BLOCK_CHARS * PROSE_BLOCK_REPEATS:
        tail = text[-PROSE_BLOCK_CHARS:]
        n = text.count(tail)
        if n >= PROSE_BLOCK_REPEATS:
            return f"block of {PROSE_BLOCK_CHARS} chars repeated x{n}"
    return None


def prose_runaway_check(text, budget_chars):
    """PURE. Reason string if this tool-call-free streamed turn should be aborted, else None.
    A turn that is (or is becoming) a tool call is never aborted: real tool_calls are tested by
    the caller; markup-style calls (<tool_call>/<function=) in the text are exempt here, so a
    large legit write_file is not cut by the budget."""
    if "<tool_call>" in text or "<function=" in text:
        return None
    if budget_chars and len(text) >= budget_chars:
        return f"no tool call after {len(text)} chars (budget {budget_chars})"
    return degenerate_repetition(text)


def turn_text_stats(text):
    """PURE. Diagnostics for a cut-off turn: chars, lines, duplicate-line share (non-blank lines
    that repeat an earlier line), and the longest repeated block measured in whole lines (the
    largest n in 1,2,4,..,64 for which some n consecutive non-blank lines occur twice)."""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    dup = (len(lines) - len(set(lines))) / len(lines) if lines else 0.0
    longest = 0
    for n in (1, 2, 4, 8, 16, 32, 64):
        if len(lines) < 2 * n:
            break
        seen, hit = set(), False
        for j in range(len(lines) - n + 1):
            w = tuple(lines[j:j + n])
            if w in seen:
                hit = True
                break
            seen.add(w)
        if not hit:
            break
        longest = n
    return {"chars": len(text or ""), "lines": len(lines),
            "dup_line_ratio": round(dup, 3), "longest_repeated_line_block": longest}


CUTOFF_CAPTURE_MAX_BYTES = 400_000


def _cap_head_tail(s, limit):
    if len(s) <= limit:
        return s
    h = limit // 2
    return (s[:h] + f"\n\n[... {len(s) - 2 * h} chars omitted by CUTOFF_CAPTURE cap ...]\n\n"
            + s[-h:])


def persist_cutoff_turn(path, *, iteration, content, thinking, finish_reason, usage, streamed,
                        abort_reason, max_tokens):
    """Write the raw cut-off / over-long no-tool-call turn (content AND reasoning) beside the
    transcript BEFORE compaction destroys it. Capped at CUTOFF_CAPTURE_MAX_BYTES (head+tail with
    a marker). Never raises. Returns the metrics dict for this turn."""
    stats = turn_text_stats(content)
    est_tokens = (usage or {}).get("completion_tokens") or max(1, (len(content or "") + len(thinking or "")) // 4)
    rec = dict(stats, iteration=iteration, completion_tokens=est_tokens,
               finish_reason=finish_reason, streamed=bool(streamed), abort_reason=abort_reason,
               thinking_chars=len(thinking or ""), max_tokens=max_tokens,
               head300=(content or "")[:300], tail300=(content or "")[-300:], file=None)
    try:
        header = (f"# cut-off turn, iteration {iteration}\n# finish_reason={finish_reason} "
                  f"streamed={bool(streamed)} abort_reason={abort_reason!r} usage={usage!r} "
                  f"max_tokens={max_tokens}\n# {stats}\n")
        per = (CUTOFF_CAPTURE_MAX_BYTES - len(header) - 200) // 2
        body = (header + "\n===== CONTENT =====\n" + _cap_head_tail(content or "", per)
                + "\n\n===== REASONING =====\n" + _cap_head_tail(thinking or "", per) + "\n")
        Path(path).write_text(body)
        rec["file"] = str(path)
    except Exception as e:
        rec["capture_error"] = str(e)[:200]
    return rec


_REASONING_ABORT_MAX_FILES = 12     # evidence files per run (bounded; a pathological run must not fill the disk)


def _persist_reasoning_abort(log_path, iteration, kind, exc, log, metrics):
    """Persist the streamed reasoning of a turn aborted by the reasoning loop / runaway guard,
    BEFORE the retry discards it. Without it nobody can later tell a true loop from a false
    positive (A/B 2026-10-12: ten fires, zero retained text). Never raises."""
    try:
        th = getattr(exc, "thinking", "") or ""
        n = metrics.get("reasoning_abort_files", 0)
        if not th or n >= _REASONING_ABORT_MAX_FILES:
            return None
        rec = persist_cutoff_turn(
            Path(log_path).with_name(f"{Path(log_path).stem}.reasoning-abort-{iteration}-{n}.txt"),
            iteration=iteration, content="", thinking=th, finish_reason="aborted", usage=None,
            streamed=True, abort_reason=f"reasoning_{kind}: {exc}", max_tokens=None)
        metrics["reasoning_abort_files"] = n + 1
        if rec.get("file"):
            log(f"[worker] aborted-turn reasoning ({len(th)} chars) saved to {rec['file']}")
        return rec
    except Exception:
        return None


def prose_runaway_nudge(budget_tokens, reason, task_kind="coding") -> str:
    if task_kind != "coding":
        return (f"Your last response was STOPPED after ~{budget_tokens} tokens with no final answer "
                f"({reason}). Do not think aloud or restate the data. Write the FINAL answer now: "
                f"concise (under 60 lines), a short table or list, from what you already read. If you "
                f"need more data, make ONE targeted tool call instead.")
    return (f"Your last response was STOPPED after ~{budget_tokens} tokens of prose with no tool "
            f"call ({reason}), so nothing was done. Do not write files, code or analysis as plain "
            f"text -- put file contents inside a write_file tool call. Make your next tool call "
            f"now, one file at a time, and keep any reasoning to a few sentences.")


def output_cap_cut_nudge(max_tokens) -> str:
    return (f"Your last response was CUT OFF at the {max_tokens}-token output limit before "
            f"you made any tool call, so nothing was done. Do not restate the analysis. Make "
            f"your next tool call now (edit the file, or read the exact lines you need), and "
            f"keep any reasoning to a few sentences.")


# --- Write-side anti-thrash (worker-write-thrash-abort) -------------------------
# The read-only EARLY ABORT is keyed on cache-serves AND a byte-for-byte unchanged
# tree, which by definition can never hold for a run that keeps WRITING files -- so
# a model re-emitting the scaffold fixture placeholder over and over (each write
# "OK") had no abort path at all and ground to max_iters with zero forward progress.
# These helpers give the write side its own per-(path, content) counter: identical
# bytes to the same path 3+ times is non-advancing by definition, and a write that
# still carries the scaffold sentinel text is non-advancing even when the model
# jiggles whitespace/comments around it -- so it collapses to ONE key per path.
HARNESS_SELFCHECK_MARKER = "VERIFY_OK: harness discriminates and is satisfiable"


HARNESS_SELFCHECK_SCRIPT = "auto-harness-check.py"


def is_harness_selfcheck_verify(stdout, stderr=None, verify_cmd=None) -> bool:
    """True when the verify that just passed is an AUTHORING SELF-CHECK.

    An auto-author / auto-refine dispatch's deliverable is a STATE -- "the harness
    discriminates and is satisfiable" -- not a diff. When the harness is already
    complete (a resumed authoring run, or a refine round enqueued for a reason
    that turned out not to exist), the correct model behaviour is to change
    NOTHING. The unchanged-tree vacuous-pass guard then failed it for doing the
    right thing: measured, 13 jobs have been failed by that guard and 10 of them
    (77%) were exactly this. A FAILED authoring slice is re-author-eligible, so
    each one seeds the retry storm MAX_AUTHOR_ATTEMPTS exists to cap.

    Deliberately keyed on the VERIFY'S OWN OUTPUT -- not the job label, not
    task_kind. Only the authoring self-check emits this marker, so a coding
    dispatch can never claim the exemption and a genuine zero-edit vacuous pass on
    real code is still caught exactly as before. That is the case this must not
    loosen, and it is what the revert-test pins.

    HARDENED 2026-09-19 after independent review. The first cut tested
    `MARKER in stdout + stderr` -- an UNANCHORED SUBSTRING of a string that is not
    secret and that ships inside every worktree: write_harness_check() writes
    auto-harness-check.py, containing this exact literal, into the same tree where
    a coding slice's verify.sh later runs. So any verify output that merely
    SURFACED that file's text -- a repo-wide grep, a lint or traceback quoting the
    line, a stray cat -- claimed the anti-fraud exemption for a genuinely vacuous
    coding pass. Two independent conditions now, both required:

      1. the marker is a WHOLE LINE of the output, not a substring of one, so
         quoting it inside a larger line (grep hits, tracebacks) no longer counts;
      2. the verify COMMAND is the harness check itself.

    (2) is the load-bearing one, and note what it keys on: WHICH INSTRUMENT RAN.
    That is not the rejected "trust the job label / task_kind" -- the label is a
    claim about the job, while the verify command is the thing actually executed to
    produce this output. A coding slice's verify is its own verify.sh and can never
    be auto-harness-check.py, so a coding dispatch cannot reach the exemption even
    if it reproduces the marker byte-for-byte on its own line.

    verify_cmd=None means the caller could not say which instrument ran, which is
    no evidence at all -- fail closed."""
    if not verify_cmd or HARNESS_SELFCHECK_SCRIPT not in str(verify_cmd):
        return False
    for stream in (stdout, stderr):
        for line in (stream or "").splitlines():
            if line.strip() == HARNESS_SELFCHECK_MARKER:
                return True
    return False


def zero_diff_verdict(task_kind, stdout, stderr, tree_changed,
                      files_modified_count, is_git_repo=True,
                      run_bash_success_count=0, verify_cmd=None) -> str:
    """The whole zero-diff decision, as ONE testable call.

    Returns: "ok" (stands as a pass), "fail" (override to FAILED -- vacuous),
    "warn" (pass, but flagged), or "exempt" (authoring self-check: a zero diff is
    the correct outcome).

    Extracted so the anti-fraud behaviour can be unit-tested and revert-tested
    directly. The lesson from the --reland-slice work applies here too: a source
    grep cannot tell a live guard from a dead branch, so the guard has to be
    reachable from a test."""
    if task_kind != "coding":
        return "ok"                       # research has no file-based deliverable
    if is_harness_selfcheck_verify(stdout, stderr, verify_cmd):
        return "exempt"
    if is_git_repo:
        if tree_changed:
            return "ok"
        return "fail" if files_modified_count == 0 else "warn"
    # degraded: no working-tree diff available
    if files_modified_count == 0:
        return "fail" if run_bash_success_count == 0 else "warn"
    return "ok"


# --- exempt-branch TARGET residue revert (2026-09-24) --------------------------
# The hole the exemption shipped with. zero_diff_verdict() returns "exempt" BEFORE
# it ever looks at tree_changed, so the exempt branch accepted the run without
# asking what the run had left in the tree. Live case:
# auto-refine-bo-O-qwen36-35b-a3b-studio-s1-fastapi-existing-route-p-r1 (job
# 765661efeb0c). A harness-refine round whose real deliverable is the fixture
# applied refimpl.py to the TARGET to prove the fixture discriminates -- which is
# legitimate and is how these rounds validate themselves -- but applied it via its
# own run_bash iterations rather than through auto-harness-check.py, whose
# try/finally revert() is the thing that normally puts the target back. The verify
# then emitted the self-check marker, this branch logged "zero-diff ACCEPTED", and
# the job completed with the TARGET holding refimpl's reference solution verbatim
# and uncommitted. The next stage's pre-flight baseline-clean gate saw `M <target>`,
# NO-GO'd and escalated the slice -- a second-order failure that reads as an
# unrelated "queue stuck" incident until it is traced back to here. This is the
# "dirty launch-baseline cascade" auto-harness-check.py's own finally-block
# comment names; that block only covers the path where the check itself applied
# the refimpl.
#
# SCOPE IS THE WHOLE DESIGN HERE, and it is deliberately NOT "restore the working
# tree to its start state". A whole-tree `git checkout -- .` at this point is the
# bg-captcha-s3-vision bug (see auto-harness-check.py's pre_tracked snapshot and
# ollama-dispatch-auto's baseline-clean blocker rewrite): once the slicer chain's
# clean_and_seal() has committed the harness into the baseline, the refine model's
# fixture / TASK.md / refimpl.py edits are TRACKED and uncommitted, so a blanket
# checkout silently destroys the round's actual deliverable while still exiting 0 --
# the model then finds its work gone with no error to explain it. The pipeline's
# own contract is "only <target>/<fixture>/refimpl.py/TASK.md may change", so this
# touches exactly ONE file, the declared target from .dispatch-harness.json, and
# never the harness files that ARE the exempt branch's stated deliverable.
#
# Every abstain is a FALSE, never an exception, and the fail-safe direction is "do
# not revert": destroying state we cannot prove was ours is strictly worse than
# leaving residue for the next pre-flight to catch loudly.
HARNESS_MANIFEST = ".dispatch-harness.json"


def snapshot_status_lines(snapshot):
    """The `git status --porcelain` block out of a _git_worktree_snapshot() string.

    None (not []) when the snapshot is missing or does not carry the delimiters --
    "I cannot tell" is a distinct answer from "nothing was dirty", and the caller
    must not collapse them."""
    if not snapshot:
        return None
    if "---STATUS---" not in snapshot or "---DIFF---" not in snapshot:
        return None
    body = snapshot.split("---STATUS---", 1)[1].split("---DIFF---", 1)[0]
    return [ln for ln in body.splitlines() if ln.strip()]


def snapshot_path_dirty(snapshot, path):
    """Was `path` already dirty at the time this snapshot was taken?

    True / False / None, where None means UNDETERMINED and the caller must abstain.
    Reuses the start snapshot the vacuous-pass guard already persists across
    --resume, so this needs no new resume-state key: the true dispatch start is
    exactly what that snapshot records.

    git quotes porcelain paths containing non-ASCII or control characters
    ("\\303\\251.py"), and un-quoting that is more failure surface than it is worth
    for a check whose job is to decide whether to DELETE someone's work. A quoted
    line we did not match therefore yields None, not False."""
    lines = snapshot_status_lines(snapshot)
    if lines is None or not path:
        return None
    saw_quoted = False
    for ln in lines:
        entry = ln[3:] if len(ln) > 3 else ""
        if entry.startswith('"'):
            saw_quoted = True
            continue
        # Renames/copies read "R  old -> new"; either side names a path that a
        # revert of `path` would be reasoning about, so both count as dirty.
        for cand in entry.split(" -> "):
            if cand.strip() == path:
                return True
    return None if saw_quoted else False


def harness_exempt_revert_decision(verdict, is_git_repo, tree_changed, target,
                                   target_dirty_at_start, target_dirty_now):
    """ONE decision: does the exempt branch restore the declared target first?

    Returns (bool, reason) -- the reason is logged and asserted on, so a future
    change of behaviour shows up as a changed reason rather than a silent one.

    Extracted for the same reason zero_diff_verdict() was: the exemption's first
    version was an inline branch, which immediately created two records of one fact.
    A source grep cannot tell a live guard from a dead one, so the decision has to
    be reachable from a test."""
    if verdict != "exempt":
        return (False, "not-exempt")
    if not is_git_repo:
        return (False, "not-a-git-repo")
    if not tree_changed:
        # The case the exemption was ADDED for: a resumed authoring run or a refine
        # round enqueued for a reason that turned out not to exist, where changing
        # nothing is correct. There is nothing to revert and nothing to log about.
        return (False, "tree-unchanged")
    if not target:
        return (False, "no-declared-target")
    if not target_dirty_now:
        return (False, "target-already-clean")
    if target_dirty_at_start is None:
        return (False, "target-start-state-unknown")
    if target_dirty_at_start:
        # The target carried uncommitted state into this dispatch, so its start
        # content is NOT HEAD and `git checkout HEAD -- <target>` would not restore
        # it -- it would overwrite work that predates this run. Leave it.
        return (False, "target-dirty-at-dispatch-start")
    return (True, "revert-target")


def declared_harness_target(cwd):
    """The `target` from .dispatch-harness.json, or None. Never raises."""
    try:
        p = Path(cwd) / HARNESS_MANIFEST
        if not p.is_file():
            return None
        t = (json.loads(p.read_text()) or {}).get("target")
        t = str(t).strip() if t else ""
        # A path that escapes the worktree, or an absolute one, is not something
        # this guard will hand to `git checkout`.
        if not t or t.startswith("/") or ".." in Path(t).parts:
            return None
        return t
    except Exception:
        return None


def _git_path_is_dirty(cwd, path):
    """True/False/None(undetermined) for one path, via git itself."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--", path], cwd=cwd,
                            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        return bool(r.stdout.strip())
    except Exception:
        return None


def _git_restore_path(cwd, path):
    """`git checkout HEAD -- path`. (True, "") on success, (False, reason)."""
    try:
        r = subprocess.run(["git", "checkout", "HEAD", "--", path], cwd=cwd,
                            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return (True, "")
        return (False, (r.stderr or r.stdout or "").strip()[-300:])
    except Exception as e:
        return (False, str(e)[-300:])


def has_scaffold_sentinel(content):
    c = str(content)
    return ("THE ONE THING THE GENERATOR CANNOT WRITE" in c
            or "CASES is empty and the verify FAILS" in c)


def write_thrash_key(path, content):
    # Sentinel-bearing writes collapse to a single per-path key so byte jiggles
    # around the placeholder cannot dodge the counter; genuine content keys are
    # (path, sha256(content)[:12]) -- any change of path or bytes yields a
    # different key, so legitimately advancing edits never collide.
    if has_scaffold_sentinel(content):
        return f"{path}:sentinel"
    return f"{path}:{hashlib.sha256(str(content).encode('utf-8')).hexdigest()[:12]}"


# --- SHADOW-MODE no-net-progress telemetry (2026-09-19) -------------------------
# NOT A BREAKER. It records and never decides -- see no_progress_track's docstring
# for why it deliberately stops short of acting.
NO_PROGRESS_SAMPLE_FLOOR = 3


def no_progress_track(state: dict, snapshot) -> dict:
    """PURE bookkeeping (mutates and returns `state`). SHADOW MODE: RECORDS ONLY.

    Counts consecutive iterations whose worktree snapshot is byte-identical to the
    previous one -- i.e. the model did things but the tree NETTED TO NOTHING.

    WHY THIS AXIS. The two existing guards are both REPETITION-keyed: the read-side
    early abort needs anti-thrash cache-serves plus a tree unchanged since dispatch
    START, and write_thrash_abort needs the same (path, content) 3x. A run that
    writes genuinely DIFFERENT bytes every iteration mints a fresh key each time and
    arms neither, which is how bg-crypto's s3 ground to a force-stop. This measures
    forward motion instead of self-similarity, so write/revert oscillation and
    churn-that-nets-to-nothing both show up.

    WHY IT DOES NOT ACT (coordinator sign-off 2026-09-19). The motivating case is
    already fixed and no longer reproducible: bg-crypto's loops (c71f0581d998,
    94cfde2bdf95) both predate the secret-redactor fix that was corrupting
    request_diagnostics, and every bg-crypto job run after it completed normally.
    Worse, the obvious threshold would FALSE-POSITIVE on correct behaviour -- a model
    that reads and analyses for several iterations before writing has a legitimately
    unchanged tree, and that is exactly what a careful run looks like early on. So
    this gathers the real distribution first; a threshold, if one is ever wanted,
    gets chosen from data instead of guessed.

    `snapshot is None` (cwd not a git work tree) records NOTHING rather than
    counting a stall -- absence of measurement is not evidence of stalling, the same
    rule the launch-baseline stamp follows.

    max_streak is a HIGH-WATER MARK: it must not decay when the model resumes making
    progress, or a run that stalled for 20 iterations and then recovered would report
    the same as one that never stalled at all."""
    if snapshot is None:
        return state
    state["samples"] = state.get("samples", 0) + 1
    if state.get("last") == snapshot:
        state["streak"] = state.get("streak", 0) + 1
    else:
        state["streak"] = 0
    state["last"] = snapshot
    state["max_streak"] = max(state.get("max_streak", 0), state["streak"])
    return state


def write_thrash_abort(_write_repeats, threshold=3):
    # (True, message-naming-the-path) when ANY single key reached the per-key
    # threshold; distinct files each written twice stay under it by construction.
    for key, n in _write_repeats.items():
        if n >= threshold:
            path = key.split(":", 1)[0]
            return (True, f"{path} was written {n}x with byte-identical or "
                          f"scaffold-sentinel content -- non-advancing write thrash")
    return False, ""


# --- Anti-thrash guard for re-issued identical READ-ONLY tool calls -------------
# Motivating failure (the owner 2026-09-08): a diagnosis dispatch re-issued near-identical
# run_bash greps against the SAME files ("Let me grep app/bfmr/page.tsx..." fired
# repeatedly), burning context going in circles. The existing loop-detect (below, in
# the tool loop) only SOFT-nudges at 3/6/9 and still RE-RUNS the command every time,
# paying full tool-output cost per repeat. This guard is complementary: on a repeat of
# an identical read-only call whose result we already have, hand back the CACHED result
# annotated ("you already ran this; act on it") WITHOUT re-running the tool, and after
# ANTI_THRASH_STRONG_AFTER repeats also emit a stronger nudge to change approach.
ANTI_THRASH_STRONG_AFTER = 3  # identical read-only repeats before escalating the nudge


def _anti_thrash_intercept(sig, cacheable, result_cache, repeat_counts):
    """Intercept a re-issued identical read-only tool call.

    `cacheable` is the caller's decision that this (name,args) is a read-only inspection
    safe to serve from cache (read_file/list_files, or a run_bash local-read -- never a
    mutation, and never the job's own verify). Returns (cached_result_or_None, nudge_or_None):
    a non-None cached_result means "short-circuit -- do NOT run the tool, use this instead".
    Mutates repeat_counts so the escalation fires once it crosses the threshold.
    """
    if not cacheable or sig not in result_cache:
        return None, None
    n = repeat_counts.get(sig, 0) + 1
    repeat_counts[sig] = n
    annotated = (
        str(result_cache[sig])
        + f"\n\n[NOTE: you already ran this exact command earlier (repeat #{n}); the "
          f"result is unchanged -- do not repeat it, act on it.]"
    )
    nudge = None
    if n >= ANTI_THRASH_STRONG_AFTER:
        nudge = (
            f"You have now re-issued the identical read-only call {n} times and its result "
            f"has not changed. STOP repeating it -- either use what you already have to "
            f"produce your final output/file NOW, or take a genuinely different action (a "
            f"different command, a different file, or a different approach)."
        )
    return annotated, nudge


# --- Reasoning-novelty ("frozen thinking") detector ----------------------------
# Motivating failure (the owner 2026-09-19, BFMR split-reservation diagnosis job
# 4b829e59598d, terminal_reason=read_thrash, exit 2, ZERO output). Post-mortem over
# the resume transcript 20260919T160454Z.json:
#
#   [20] think=eb4d1d9965 len=2297  read_file lib/bfmrWeb.ts + ls lib | grep -i join
#   [23] think=049238f41c len=2296  read_file lib/bfmrSplitSibling.test.ts
#   [25] think=eb4d1d9965 len=2297  read_file lib/bfmrReservationLineKey.test.ts
#   [28] think=eb4d1d9965 len=2297  read_file lib/bfmrVerify.test.ts   <- first
#   [30][33][35][38][42][45] all eb4d1d9965, all read_file lib/bfmrVerify.test.ts
#
# [20] and [25] are byte-identical; [23] differs from [20] by ONE character ("core
# issue now -- when" vs "core issue now: when"). So the model's reasoning was a
# fixed point from [20] onward -- roughly SIX iterations before the first repeated
# tool signature at [30], which is the earliest moment the signature-keyed
# loop-detect/anti-thrash above can possibly notice anything. [20]->[28] are four
# DIFFERENT tool signatures derived from one frozen reasoning state, so the existing
# detectors are structurally blind to them.
#
# This detector watches the signal that actually went stale: `message["thinking"]`,
# which the worker already captures (see _save_transcript / the streaming reader) and
# until now never used for anything but display. Two consecutive near-identical
# reasoning blocks is not a style quirk, it is a decode fixed point -- the model is
# re-deriving the same next action forever and no amount of appended prose will move
# it (see the harvest helpers below for why the run is not simply discarded).
#
# Deliberately conservative, so this can never pathologise a working run:
#   * blocks shorter than REASONING_FREEZE_MIN_CHARS are ignored and RESET the streak
#     -- a terse "Let me check the tests." repeated twice is not a fixed point, and
#     models run with --think off emit no thinking at all and are unaffected entirely.
#   * comparison is on a punctuation/whitespace/case-normalised form at
#     REASONING_FREEZE_SIMILARITY, so the one-character [20]->[23] drift above counts
#     as a repeat instead of resetting the counter (that single char is exactly how
#     this failure evaded a naive exact-hash check).
#   * the FIRST repeat only nudges (same idiom as loop-detect's 3x); the hard stop
#     needs REASONING_FREEZE_AFTER consecutive repeats. On the BFMR transcript that
#     nudges at [23] and stops at [25] -- ~5 iterations before the real abort at 18/31.
REASONING_FREEZE_AFTER = 2      # consecutive near-identical thinking blocks -> hard stop
REASONING_FREEZE_MIN_CHARS = 200  # ignore (and reset on) blocks too short to be a fixed point
REASONING_FREEZE_SIMILARITY = 0.98  # normalised-similarity floor for "the same reasoning"


def _reasoning_norm(text):
    """Punctuation/case/whitespace-insensitive form of a thinking block.

    Normalising before comparison is load-bearing, not cosmetic: the BFMR run's
    [20]->[23] pair differed only by an em-dash vs a colon, which an exact hash
    reads as novel reasoning and this reads as the repeat it actually was."""
    t = re.sub(r"[^\w\s]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def reasoning_repeat_streak(thinking, prev_norm, streak,
                            min_chars=REASONING_FREEZE_MIN_CHARS,
                            similarity=REASONING_FREEZE_SIMILARITY):
    """Advance the frozen-reasoning counter for one assistant turn.

    Returns (new_streak, new_prev_norm, is_repeat). new_streak counts CONSECUTIVE
    near-identical blocks seen so far (0 = novel/absent reasoning, 1 = first repeat,
    2 = second repeat => frozen). Pure function of its arguments so it is directly
    testable against a real transcript -- see --self-test."""
    cur = _reasoning_norm(thinking)
    if len(cur) < min_chars:
        # No usable reasoning this turn. Reset rather than carry the streak across
        # a gap: a model that alternates real reasoning with silence is not frozen,
        # and a --think off run must never trip this at all.
        return 0, "", False
    if prev_norm:
        if cur == prev_norm:
            return streak + 1, cur, True
        # SequenceMatcher on a ~2KB block is sub-millisecond and runs once per
        # iteration; quick_ratio() first so the O(n*m) path is only paid when the
        # blocks are already plausibly similar.
        sm = difflib.SequenceMatcher(None, prev_norm, cur)
        if sm.quick_ratio() >= similarity and sm.ratio() >= similarity:
            return streak + 1, cur, True
    return 0, cur, False


# --- Intra-turn reasoning-loop abort (the owner 2026-09-20) ----------------------
# Same fixed-point failure as the frozen-reasoning detector above, but caught
# WHILE a single turn is still generating instead of only after two full turns
# complete. Not every occurrence is a verbatim repeat -- bece829d8005 oscillated
# between near-identical fragments, but 0971afaccfa9 wandered across DIFFERENT
# topics (mutation analysis, docstrings, fixture cases...) for 6 real hours in
# one iteration without ever repeating text. A text-similarity check alone
# would miss that case, so this pairs two independent triggers:
#   1. repeat: the tail of think_buf already occurred earlier in the SAME
#      buffer (catches the verbatim/near-verbatim fixed point).
#   2. runaway: think_buf has grown past INTRA_TURN_MAX_THINK_CHARS without
#      the turn ever producing content or a tool call (catches wandering
#      stalls that never repeat text but also never converge on an answer).
# Both are pure functions of the accumulating buffer so they are directly
# testable against a saved transcript -- see --self-test.
INTRA_TURN_CHECK_INTERVAL = 2000    # chars of new thinking between checks (cheap: O(n) scan, not every chunk)
INTRA_TURN_TAIL_WINDOW = 400        # chars of the tail compared against everything earlier
INTRA_TURN_SIMILARITY = 0.92        # lower than the cross-turn 0.98: intra-turn phrasing drifts more within one fixed point
INTRA_TURN_MAX_THINK_CHARS = 60000  # ~15-20K tokens of unbroken thinking with zero content/tool_calls is never legitimate forward progress
# INTRA_TURN_REPEAT_MIN_EARLIER (2026-10-12, REASONING_PARSER A/B): the "repeat" trigger used to
# fire when the 400-char tail occurred ONCE earlier. Measured offline over every saved assistant
# thinking block >= 2000 chars in ~/bin/ollama-worker-logs (2454 blocks): that rule fires on 134;
# in 17 of those the repeated segment never recurs after the fire point (the model left it on its
# own and went on to write the file / call the tool -- e.g. 20260914T062918Z it20 drafted a whole
# function twice, then write_file; 20260821T214050Z it4 restated the same find command twice, then
# ran it). Requiring >=2 earlier occurrences (3 in total) removes 16 of the 17 and requiring >=3
# removes all 17, while the 113 fires whose cycle continues to the end of the block stay. >=3 chosen
# (0 measured false fires); the 24000-char reasoning budget still backstops a slower cycle.
INTRA_TURN_REPEAT_MIN_EARLIER = 3
REASONING_LOOP_MAX_RETRIES = 1      # retries of the SAME turn (with a temperature bump) before the thinking-off retry

# --- Output cap spent entirely on thinking (2026-09-27) ---------------------------
# Measured: 8 of 876 iterations across the last 60 queue logs (cb21d014c3cd it5,
# 86ff5802626b it6, 8dd2509d9bb8 it4, 3cfd791c806c it6, 96fded083c83 it5, 7c11eee182fe
# it5, 52eab236aa2f it18, 9104f6b65728 it16) logged "generated 8192 tokens in ~520-620s"
# with ZERO content and ZERO tool calls. The live logs show every one was thinking the
# whole way: num_predict (DEFAULT_MAX_TOKENS) is ONE budget shared by qwen3.8's
# `thinking` and its answer, so a hard reasoning step (cb21d: per-mutant analysis of a
# relevance report -- coherent, not a loop) used all of it and never emitted the action.
# The worker then (a) stripped that thinking from the replayed transcript, (b) treated
# the turn as a model that CHOSE to answer blank -- the generic one-shot "your last
# response was empty" nudge -- so the next turn re-derived the lost ~10 min of
# reasoning from scratch, and (c) had no nudge left for a second truncation, which
# would then be accepted as a blank final answer.
# Fix: recognise the truncation (done_reason == "length", or completion == the cap,
# with nothing emitted) as a BUDGET event, not a model choice: hand the tail of the
# lost reasoning back so it is not re-derived, tell the model to act now, and give
# that one recovery turn a larger num_predict -- capped so prompt + completion stays
# under CONTEXT_REVIEW_THRESHOLD, so a recovery turn can never trip the context gate
# on its own. Bounded to THINK_CAP_RECOVERY_MAX consecutive recoveries, then the
# ordinary empty-answer handling applies. Never consumes the empty-answer nudge.
# OUTPUT-CAP PROSE LOOP (2026-10-02, Rivian s4 author 368bc923a303): EIGHT consecutive
# turns (iters 11-18) of exactly 8192 tokens of the same prose, no tool call, each
# answered by output_cap_cut_nudge -- ~25 min of decode, +8k ctx per turn, until the
# context pause fired and the queue resumed it at a BIGGER window to keep looping.
# The nudge gets this many tries; the next consecutive cut stops the run.
OUTPUT_CAP_CUT_MAX = 2
# PROSE-LOOP DENSITY (2026-10-06, BFMR TLS refine 77d808c3984a: 12 cut-offs in 82
# iterations, ~46 of ~128 min of generation, >2h wall). The consecutive-cut streak
# above resets on ANY tool call, so a cut / one read_file / cut / cut ... rhythm
# never trips it. Density catches that rhythm: >= PROSE_LOOP_CUTS cut-off turns
# within the last PROSE_LOOP_WINDOW iterations stops the run. Back-tested on every
# job log with a cut-off (2026-10-06): fired on 13 failed runs, 0 of 93 passing.
PROSE_LOOP_WINDOW = 10
PROSE_LOOP_CUTS = 4
# WALL BUDGET (2026-10-06, the owner "refine shouldn't take hours"): an auto-generated
# authoring / refine / continuation job gets a wall-clock cap per worker process.
# Passing auto author/refine runs: p50 7 min, p90 20, p99 42.5 (602 runs).
# WORKER_WALL_BUDGET_S overrides (0 disables); other tasks are uncapped.
WALL_BUDGET_DEFAULT_S = {"author": 3600, "refine": 3600, "continue": 3600}


def harness_task_kind(task):
    """PURE. 'author' | 'refine' | 'continue' for a task the auto driver generated
    (identified by the fixed header it writes), else None."""
    head = (task or "").lstrip()[:80]
    if head.startswith("# AUTHORING TASK"):
        return "author"
    if head.startswith("# REFINE TASK"):
        return "refine"
    if head.startswith("# CONTINUE --"):
        return "continue"
    return None


def wall_budget_s(task, env=None):
    """Seconds this run may take, or None for uncapped."""
    env = os.environ if env is None else env
    raw = (env.get("WORKER_WALL_BUDGET_S") or "").strip()
    if raw:
        try:
            v = float(raw)
            return v if v > 0 else None
        except ValueError:
            pass
    return WALL_BUDGET_DEFAULT_S.get(harness_task_kind(task))


def prose_loop_tripped(cut_iters, i, window=None, cuts=None):
    """PURE. True when >= cuts output-cap cut-offs fell in iterations (i-window, i]."""
    window = PROSE_LOOP_WINDOW if window is None else window
    cuts = PROSE_LOOP_CUTS if cuts is None else cuts
    return sum(1 for c in cut_iters if i - window < c <= i) >= cuts
# Recovery after a cut (2026-10-04, output_cap_loop on Darkbloom): the cut turn is
# compacted before it goes back into context (compact_cut_off_prose), and the NEXT
# turn alone samples off the loop -- temperature >= REASONING_LOOP_RETRY_TEMPERATURE
# (a near-greedy resend of the same context reproduces the same cycle: 984db5b6a535
# and 06c2bf413cd7 emitted the identical 38260-char first cut) and a stronger
# repetition penalty. Normal turns are unchanged.
OUTPUT_CAP_CUT_KEEP_CHARS = 2000
OUTPUT_CAP_RECOVERY_REPEAT_PENALTY = 1.2
THINK_CAP_RECOVERY_MAX = 2        # consecutive truncated-in-thinking turns we recover from
THINK_CAP_TAIL_CHARS = 4000       # ~1k tokens of the lost reasoning handed back
THINK_CAP_BUDGET_FACTOR = 2       # recovery turn's num_predict = factor x the normal cap...
THINK_CAP_CTX_MARGIN = 512        # ...but prompt + budget + margin <= review threshold


def think_cap_truncated(msg, usage, done_reason, max_tokens):
    """PURE. True when this turn emitted nothing (no content, no tool_calls) because the
    output cap ran out, i.e. it stopped on length rather than by choice."""
    if (msg.get("content") or "").strip() or msg.get("tool_calls"):
        return False
    if str(done_reason or "").lower() == "length":
        return True
    ct = (usage or {}).get("completion_tokens") or 0
    return bool(max_tokens) and ct >= max_tokens


def think_cap_recovery_budget(max_tokens, prompt_tokens, num_ctx,
                              factor=THINK_CAP_BUDGET_FACTOR, margin=THINK_CAP_CTX_MARGIN):
    """PURE. num_predict for the recovery turn: factor x max_tokens, capped so that
    prompt_tokens + budget + margin stays within CONTEXT_REVIEW_THRESHOLD x num_ctx (the
    post-call context gate counts completion tokens). Never below max_tokens; with no
    num_ctx known it just returns max_tokens."""
    if not max_tokens:
        return max_tokens
    want = int(max_tokens * factor)
    if not num_ctx:
        return max_tokens
    room = int(num_ctx * CONTEXT_REVIEW_THRESHOLD) - int(prompt_tokens or 0) - margin
    return max(max_tokens, min(want, room))


def think_cap_nudge(thinking, max_tokens, tail_chars=THINK_CAP_TAIL_CHARS):
    """The recovery message: why the turn was empty, the tail of the lost reasoning,
    and the instruction to act on it now instead of re-deriving it."""
    t = (thinking or "").strip()
    tail = t[-tail_chars:]
    cut = len(t) > len(tail)
    body = (f"Your last turn used the entire {max_tokens}-token output budget on reasoning "
            f"and was cut off before you emitted anything -- no tool call, no text. That is a "
            f"budget limit, not a mistake on your part.")
    if tail:
        body += (" Here is the end of that reasoning" + (" (earlier part omitted)" if cut else "")
                 + " so you do not have to redo it:\n\n<<<REASONING\n" + tail + "\nREASONING>>>\n\n")
    else:
        body += " "
    body += ("Do NOT re-derive it. Act on it now: make your next tool call (or task_complete) "
             "in this turn, and keep any further reasoning short -- work through the rest one "
             "concrete step per turn.")
    return body
REASONING_LOOP_RETRY_TEMPERATURE = 0.4  # a temp=0 retry of a frozen decode reproduces itself deterministically


def _count_overlapping(hay, needle, stop_at):
    """Occurrences of needle in hay, overlaps counted (a cycle shorter than the needle repeats
    at spacings < len(needle), which str.count would skip); stops early at `stop_at`."""
    c, i = 0, hay.find(needle)
    while i != -1:
        c += 1
        if c >= stop_at:
            break
        i = hay.find(needle, i + 1)
    return c


def think_buf_loop_check(think_buf, last_checked_len, has_output):
    """Pure check run periodically as one turn's thinking streams in.

    `think_buf`: the full accumulated thinking text for the CURRENT turn only.
    `last_checked_len`: len(think_buf) at the previous check (caller-owned; a
      cheap gate so this only runs every INTRA_TURN_CHECK_INTERVAL chars, not
      every streamed chunk).
    `has_output`: whether this turn has produced any content or tool_calls yet.

    Returns (should_check_now, is_loop, new_checked_len, reason). Caller should
    only act on is_loop when should_check_now is True (it always is when
    is_loop is True -- the split just lets the caller skip the string work
    entirely between checkpoints)."""
    n = len(think_buf)
    if n - last_checked_len < INTRA_TURN_CHECK_INTERVAL:
        return False, False, last_checked_len, None
    if has_output:
        # Real progress this turn (content or a tool call already emitted) --
        # not a stall, no matter how much thinking preceded it.
        return True, False, n, None
    if not has_output and n >= INTRA_TURN_MAX_THINK_CHARS:
        return True, True, n, "runaway"
    norm = _reasoning_norm(think_buf)
    if len(norm) >= INTRA_TURN_TAIL_WINDOW * 2:
        tail = norm[-INTRA_TURN_TAIL_WINDOW:]
        earlier = norm[:-INTRA_TURN_TAIL_WINDOW]
        # The tail must have already occurred INTRA_TURN_REPEAT_MIN_EARLIER times: one earlier
        # occurrence is how a model legitimately drafts the same function/command twice in its
        # reasoning before writing it out (measured: INTRA_TURN_REPEAT_MIN_EARLIER comment above).
        # (The old SequenceMatcher branch compared a 400-char tail against the WHOLE earlier
        # buffer, so ratio <= 2*400/(len(earlier)+400) -- unreachable past ~1.2K chars; removed.)
        if _count_overlapping(earlier, tail, INTRA_TURN_REPEAT_MIN_EARLIER) >= INTRA_TURN_REPEAT_MIN_EARLIER:
            return True, True, n, "repeat"
    return True, False, n, None


# --- Harvest-on-abort (the owner 2026-09-19: "we should always improve") ------------
# The same BFMR run above died with terminal_reason=read_thrash and produced NOTHING:
# every assistant turn's `content` was the empty string (the model spoke only through
# tool calls), so _extract_final_answer in ollama-queue.py found no assistant text and
# the job wrote no <id>.answer.md. Yet the frozen `thinking` block it died holding was
# a real, substantially-correct partial diagnosis -- split rows share reserved_at /
# item / order_id so the join key collides, the upsert key is userId_lineKey, and the
# open question is whether BFMR's web surface returns two rows or one collapsed qty=8
# row. That was thrown away and the whole dispatch re-run from scratch.
#
# A thrash abort is a stop, not a reason to discard what the run already worked out.
# So on a non-converged THRASH/FREEZE abort that left no assistant text at all, recover
# the last substantive reasoning block and append it as a clearly-labelled assistant
# message. That single append makes the whole existing pipeline carry it:
# ollama-queue.py's _extract_final_answer walks the transcript for the last assistant
# turn with non-empty content, so the job gets its durable <id>.answer.md and its
# ANSWER.md for free, with no queue-side change at all.
#
# The banner is NOT optional politeness. Harvested reasoning is a model's private
# scratchpad -- mid-thought, frequently self-contradictory, and by construction from a
# run that FAILED. It must never be readable as a finished answer. The run stays
# converged=False with its terminal_reason intact either way: this changes what a
# failed run LEAVES BEHIND, never whether it failed.
HARVEST_MIN_CHARS = 200


def harvest_reasoning(messages, min_chars=HARVEST_MIN_CHARS):
    """The last substantive `thinking` block in the transcript, or None."""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        th = m.get("thinking") or m.get("reasoning") or ""
        if isinstance(th, str) and len(th.strip()) >= min_chars:
            return th.strip()
    return None


def _msg_text(m):
    """An assistant message's textual content, tolerating the list-of-parts shape."""
    c = (m or {}).get("content")
    if isinstance(c, list):
        c = "\n".join(p if isinstance(p, str)
                      else (p.get("text") or "") if isinstance(p, dict) else ""
                      for p in c)
    return c.strip() if isinstance(c, str) else ""


def run_ended_without_answer(messages):
    """True when the run's LAST assistant turn carried no text -- i.e. it stopped
    mid-tool-loop and never said anything final.

    Why "last turn" and not "any turn" (learned the hard way writing the test for
    this): the BFMR transcript is NOT textless. It contains exactly one non-empty
    assistant content in 48 messages -- message [8], "Found the 409 throw site. Now
    let me find the sync-from-BFMR code path." -- a 71-character progress note from
    early exploration. ollama-queue.py's _extract_final_answer walks the transcript
    in REVERSE for the last assistant turn with text, so that throwaway line is what
    the job would have persisted as its ANSWER.md. An "any assistant text at all"
    guard would therefore have suppressed the harvest on the very run that motivated
    it, and left that one-liner standing as the answer. Keying on the FINAL turn is
    what makes the harvest additive: it appends after the stale note, so the reverse
    walk finds the harvested block instead."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return not _msg_text(m)
    return True


def build_harvested_answer(reason, reasoning):
    """Wrap a harvested reasoning block so it cannot be mistaken for a real answer."""
    return (
        "HARNESS-HARVESTED PARTIAL ANSWER -- THIS DISPATCH DID NOT CONVERGE "
        f"(early_abort={reason}).\n\n"
        "The run was stopped early for non-productive repetition and never produced a "
        "final answer of its own. What follows is the model's last internal reasoning "
        "block, recovered verbatim by the harness so the work is not lost.\n\n"
        "TREAT IT AS A LEAD, NOT A FINDING: it is mid-thought scratch work from a run "
        "that FAILED, it may contradict itself, and nothing in it has been verified. "
        "Confirm every claim against the code before acting on it.\n\n"
        "---\n\n" + str(reasoning).strip()
    )


# --- Resume-time transcript compaction (the owner 2026-09-08) ------------------------
# A job that paused at ~92% context leaves a resume= transcript, but resuming INTO a
# ~92%-full window can't make progress -- the pre-send projection re-pauses almost
# immediately, so a plain resume is inert. (The queue's auto-resume bumps num_ctx
# UPWARD, but that has a host ceiling.) Compaction makes the resumed run start with
# real headroom: keep the task spec + verify identity + the most-recent tool results,
# and replace the earlier exploration with a compact summary of what was already done.
RESUME_COMPACT_TARGET = 0.70    # after compaction, aim below this fraction of num_ctx
RESUME_COMPACT_KEEP_TAIL = 8    # most-recent messages kept verbatim (the model needs these)
RESUME_COMPACT_SUMMARY_CAP = 4000  # max chars of the exploration summary


def _estimate_transcript_tokens(messages):
    """Same bytes/4 heuristic the pre-send accumulation guard uses (kept in sync by
    value with that inline `len(text) // 4`)."""
    return sum(len(str(m.get("content") or "")) for m in messages) // 4


def _summarize_exploration(dropped, cap=RESUME_COMPACT_SUMMARY_CAP):
    """Collapse the dropped middle of a transcript into ONE compact user message that
    lists what was already explored (tool calls + truncated results), so the resumed
    model does not repeat those reads. Returns a message dict, or None when there is
    nothing to summarize."""
    if not dropped:
        return None
    lines = []
    for msg in dropped:
        role = msg.get("role")
        content = str(msg.get("content") or "").strip().replace("\n", " ")
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            lines.append(f"- called {fn.get('name')}({str(fn.get('arguments'))[:120]})")
        if not content:
            continue
        if role == "assistant":
            lines.append(f"- reasoned: {content[:160]}")
        else:  # tool / user (tool results in either message shape)
            lines.append(f"- result: {content[:160]}")
    body = "\n".join(lines)
    if len(body) > cap:
        body = body[:cap] + "\n- ...(earlier exploration truncated)"
    return {"role": "user", "content":
            "[CONTEXT COMPACTED ON RESUME] Earlier exploration in this run was summarized to "
            "free up context. You have ALREADY done the following -- do NOT repeat these "
            "reads/searches, act on what you found:\n" + body +
            "\n\nProceed to produce your final output/file now."}


def _compact_resumed_transcript(messages, num_ctx,
                                review_threshold=CONTEXT_REVIEW_THRESHOLD,
                                target=RESUME_COMPACT_TARGET,
                                keep_tail=RESUME_COMPACT_KEEP_TAIL):
    """Compact a paused transcript so a resume starts with headroom.

    Returns (new_messages, compacted, before_tokens, after_tokens). Leaves the transcript
    byte-for-byte UNCHANGED (compacted=False) when num_ctx is unknown or the transcript
    already fits comfortably below review_threshold -- so a resume that has room is never
    disturbed. When it is near/over the ceiling, preserve the system prompt (msg 0) and the
    original task (first user message) VERBATIM -- the task spec, target-file identity and
    verify live there and must never be dropped -- keep the most-recent `keep_tail` messages
    verbatim, and replace everything between with one summary. If a single kept message is
    itself huge, trim the tail (never below the last result) until under `target`.
    """
    if not num_ctx:
        return messages, False, 0, 0
    before = _estimate_transcript_tokens(messages)
    if before < num_ctx * review_threshold:
        return messages, False, before, before
    rest = list(messages)
    head = []
    if rest and rest[0].get("role") == "system":
        head.append(rest.pop(0))
    if rest and rest[0].get("role") == "user":
        head.append(rest.pop(0))  # the original task spec (verify + target identity)
    tail = rest[-keep_tail:] if keep_tail > 0 else []
    middle = rest[:-keep_tail] if keep_tail > 0 else rest
    summary = _summarize_exploration(middle)
    new_messages = head + ([summary] if summary else []) + tail
    after = _estimate_transcript_tokens(new_messages)
    while after >= num_ctx * target and len(tail) > 1:
        tail = tail[1:]
        new_messages = head + ([summary] if summary else []) + tail
        after = _estimate_transcript_tokens(new_messages)
    return new_messages, True, before, after


def _context_budget_nudges(total_tokens_used, num_ctx, already_fired,
                           thresholds=CONTEXT_NUDGE_THRESHOLDS,
                           review_threshold=CONTEXT_REVIEW_THRESHOLD):
    """Proactive mid-run context-budget nudges (see CONTEXT_NUDGE_THRESHOLDS).

    Returns a list of (threshold, message) for each budget threshold NEWLY crossed
    this iteration, and mutates `already_fired` (a set) so each threshold fires at most
    once per run. Only thresholds strictly below review_threshold are considered -- the
    0.90 pause supersedes the top nudge. Pure (no I/O) so it is unit-testable and proves
    red-on-revert.
    """
    out = []
    if not num_ctx:
        return out
    for thr in thresholds:
        if thr >= review_threshold:
            continue
        if total_tokens_used >= num_ctx * thr and thr not in already_fired:
            already_fired.add(thr)
            pct = total_tokens_used / num_ctx
            out.append((thr,
                f"[Context budget: you are at {pct:.0%} of your context window "
                f"({total_tokens_used}/{num_ctx} tokens). You have limited room left -- "
                f"converge and produce your final output/file NOW rather than exploring "
                f"further. Do NOT re-read files you have already seen; act on what you have.]"))
    return out


def run_task(model, host, cwd, task, verify, max_iters, temperature, num_ctx, searxng_host,
             system_prompt_file=None, cleanup_after=False, manual_tools=False,
             top_p=None, top_k=None, api_style="ollama", claude_prep_tokens=None,
             resume_from=None, task_kind="coding", chat_timeout=CHAT_TIMEOUT_S,
             max_tokens=None, repeat_penalty=None,
             facts_provided=False, web_fetch_max_chars=None, read_file_max_chars=None,
             verify_failed_at_baseline=False, scored_arm=False, num_ctx_bumps=0,
             min_web_fetches=0,
             live_log=None, dispatch_tag=None, think=None, capture_final_as=None,
             preserve_reasoning=False, role="author", extra_iterations=0, mode="open",
             fixed_targets=None, budget_policy=None, prefetch_specs=None):
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    # BUDGET POLICIES (worker_budget.py): CLI > `Budget-policy:` line in the task > env
    # WORKER_BUDGET_POLICY; empty (the default) leaves every code path below untouched.
    _bp_names, _bp_unknown, _bp_source = (frozenset(), [], "default")
    if _bb is not None:
        _bp_names, _bp_unknown, _bp_source = _bb.resolve_policies(budget_policy, None, task)
    if _bp_unknown:
        log(f"[worker] budget policy: ignoring unknown name(s) {_bp_unknown} (known: {', '.join(_bb.POLICIES)})")
    if _bp_names and task_kind != "coding":
        log(f"[worker] budget policy {sorted(_bp_names)} ignored: task kind is {task_kind!r}, policies apply to coding runs only.")
        _bp_names = frozenset()
    _READ_WINDOW[0] = _bb.READ_WINDOW_LINES if "read_window" in _bp_names else 0
    # MODEL PROFILE (model_profiles.yaml): any sampling/budget value the caller left None
    # comes from the model card for (model, role); explicit CLI values win. Resolved to
    # concrete numbers here so every downstream use (budget math, recovery bumps, the
    # request builders) sees the same value.
    # Per-job A/B control arm, no queue change: a `Sampling-profile: legacy` line in the task text sets
    # DISPATCH_SAMPLING=legacy for this worker process (model_profile.get_profile -> pre-2026-10-08 values).
    _sp_m = re.search(r"(?im)^[ \t]*Sampling-profile:[ \t]*(legacy|card)[ \t]*$", task or "")
    if _sp_m and not os.environ.get("DISPATCH_SAMPLING"):
        os.environ["DISPATCH_SAMPLING"] = _sp_m.group(1).lower()
        log(f"[worker] SAMPLING PROFILE {_sp_m.group(1).lower()} (from the task's Sampling-profile line).")
    if _wl is not None:     # per-job opt-in levers via a `Worker-flags:` task line (allow-listed; none = unchanged)
        _wl.apply_task_flags(task, log=log)
    _prof = _mp.get_profile(model, role)
    log(f"[worker] model profile: {_prof['profile_key']} mode={_prof['mode']} (role={role}) "
        f"temp={_prof['temperature']} top_p={_prof['top_p']} top_k={_prof['top_k']} "
        f"presence={_prof['presence_penalty']} rep={_prof['repetition_penalty']} "
        f"max_tokens={_prof['max_tokens']} enable_thinking={_prof['enable_thinking']} "
        f"tool_calls={_prof['tool_call_format']}"
        + (" FALLBACK (no model card)" if _prof["fallback"] else "") + "; explicit flags override")
    _dispatch_metrics["model_profile"] = f"{_prof['profile_key']}:{_prof['mode']}"
    # Optional bash-only action mode (mini-swe-agent; profile robust.bash_only, OFF by default):
    # one ```bash block per turn is converted to a run_bash call; `echo TASK_COMPLETE` becomes
    # task_complete (so every gate still applies). Rides the existing manual-tools plumbing.
    _bash_only_mode = bool((_prof.get("robust") or {}).get("bash_only")) and task_kind == "coding"
    if _bash_only_mode:
        manual_tools = True
        log("[worker] BASH-ONLY action mode (profile robust.bash_only): one ```bash block per turn.")
    if temperature is None:
        temperature = _prof["temperature"]
    if top_p is None:
        top_p = _prof["top_p"]
    if top_k is None:
        top_k = _prof["top_k"]
    if max_tokens is None:
        max_tokens = _prof["max_tokens"]
    if repeat_penalty is None:
        repeat_penalty = _prof["repetition_penalty"]
    if num_ctx is None:
        num_ctx = _prof["ctx"]
    num_ctx = clamp_unraid_ctx(host, model, num_ctx)
    tool_impls = build_tool_impls(searxng_host, web_fetch_max_chars=web_fetch_max_chars,
                                  read_file_max_chars=read_file_max_chars, num_ctx=num_ctx,
                                  verify=verify)

    # External graceful pause support -- see _sigterm_pause_handler above. Installed here,
    # BEFORE model warmup (which can take minutes on a cold load), so a SIGTERM arriving any
    # time after this point pauses the run instead of killing it with the default handler.
    # The flag is honored at the end of each completed iteration in the loop below.
    signal.signal(signal.SIGTERM, _sigterm_pause_handler)

    # Advisory only (Fable review, 2026-08-29) -- a negative-grep verify guard
    # ('! grep ... pattern') can't distinguish a forbidden command being EXECUTED
    # from that same text merely being printed/echoed/commented (confirmed real
    # incident the same day: a guard against editing /etc/pam.d failed correct
    # work that only PRINTED the command for a human to run manually). Not
    # something the harness can safely auto-correct -- just flag it so whoever's
    # reading the log notices before trusting a FAILED verdict from one of these.
    if verify and re.match(r"^\s*!\s*grep", verify):
        log(f"[worker] NOTE: --verify looks like a negative-grep guard ({verify!r}) -- these "
            f"can't distinguish a forbidden pattern being EXECUTED from it merely being "
            f"echoed/printed/commented. If this fails, check whether the match is real before "
            f"trusting VERIFY FAILED.")

    # --resume: added 2026-08-28 after killing an in-progress dispatch
    # (the research test) to free the host for an urgent fix and losing all
    # its progress -- Ollama's inference is stateless per-request (the full
    # message history gets resent every call), so the dispatch's real state
    # is just this `messages` list, not something living inside the model.
    # Saving it incrementally (below) and reloading it here is the whole
    # mechanism: unload model A mid-task, load model B for something urgent,
    # unload B, reload A, resume A's messages and let it finish -- no
    # progress lost. Model/host/task are still taken fresh from the CLI args
    # (not read back out of the saved file), so resuming with a DIFFERENT
    # model than started the task is a deliberate, supported case, not an
    # error -- that's the actual pause-work-resume workflow this exists for.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if resume_from:
        resume_path = Path(resume_from).resolve()
        saved = json.loads(resume_path.read_text())
        messages = saved["messages"]
        resumed_at_iteration = saved.get("iterations", 0)
        # WHY this reason is carried into the budget computation (see total_iters
        # below): a resume is not always a GRANT. A deliberate grant
        # (`request_more_iterations`, reviewed and resumed with more budget)
        # should extend. An INVOLUNTARY pause -- an external SIGTERM from the
        # queue daemon preempting or kickstarting -- granted nothing, and must
        # not silently buy another full budget.
        resumed_pause_reason = saved.get("pause_reason")
        log_path = resume_path  # keep updating the same file across pause/resume cycles
        # Preserve the FULL restored transcript for the vacuous-pass counter reconstruction
        # below (it scans messages for OK:wrote / exit_code markers): compaction may drop the
        # middle where those markers live, so the counters must be rebuilt from the original.
        _resume_full_messages = list(messages)
        # Resume-time compaction (see _compact_resumed_transcript): a resume into a near-full
        # window is otherwise inert. No-op when the transcript already has room.
        messages, _compacted, _c_before, _c_after = _compact_resumed_transcript(messages, num_ctx)
        if _compacted:
            log(f"[worker] resume compaction: transcript ~{_c_before} tok "
                f"({_c_before / num_ctx:.0%}) -> ~{_c_after} tok ({_c_after / num_ctx:.0%}) of "
                f"{num_ctx} -- kept the task/verify + recent results, summarized earlier "
                f"exploration so the resumed run starts with headroom.")
        log_path = resume_path
        log(f"[worker] resuming from {resume_path} (was at iteration {resumed_at_iteration}, "
            f"{len(messages)} messages) -- now dispatching to {model} on {host}")
        if manual_tools:
            log("[worker] --manual-tools: native tool_calls bypassed, using textual tool-schema "
                "injection + our own response parsing instead (see render_manual_tools_block).")
    else:
        system_prompt = RESEARCH_SYSTEM_PROMPT if task_kind == "research" else SYSTEM_PROMPT
        if task_kind != "research":
            # Opus root-cause, 2026-09-20 (bonsai bake-off rep2 hard-zero, iter_cap,
            # 0/49 -- transcript ...unraid-llamaserver-ternary-r2.log): the prompt told
            # the model to keep paths "relative to the working directory" but never
            # once stated what that directory WAS. This model ran `cd / ; pwd` on its
            # first run_bash call, read the printed `/` as fact, and burned 26 of 30
            # iterations (720s in three 240s `find /` timeouts) hunting for a worktree
            # it was already sitting in -- zero edit_file calls, zero reasoning
            # repetition, so neither frozen-reasoning detector could see it: this was a
            # SEMANTIC loop (wrong belief about cwd) not a textual one. Stating the
            # absolute path up front, and that a `cd` in one run_bash call never
            # persists to the next (each call gets a fresh subprocess at this same
            # cwd -- see tool_run_bash), removes the false premise before it can form.
            system_prompt = system_prompt + (
                f"\n\nYour working directory is: {cwd}\nAll relative paths in your tool "
                f"calls are resolved against this directory. Each run_bash call starts a "
                f"NEW shell process at this same directory -- a `cd` inside one run_bash "
                f"call does NOT persist to the next call, so `pwd` will always print this "
                f"path again next time regardless of any `cd` you ran before. Do not "
                f"search the filesystem to relocate this directory; you are always "
                f"already in it.")
        if system_prompt_file:
            system_prompt = Path(system_prompt_file).read_text()
            log(f"[worker] using custom system prompt from {system_prompt_file}")
        elif task_kind == "research":
            log("[worker] --task-kind research: using RESEARCH_SYSTEM_PROMPT (not the coding one).")
        if _bash_only_mode:
            system_prompt = (system_prompt + "\n\nBASH-ONLY MODE: do NOT emit tool calls -- the tool "
                             "list above is replaced by this rule. " + _wr.BASH_MODE_PROMPT)
        elif manual_tools:
            system_prompt = system_prompt + "\n\n" + render_manual_tools_block(TOOLS)
            log("[worker] --manual-tools: native tool_calls bypassed, using textual tool-schema "
                "injection + our own response parsing instead (see render_manual_tools_block).")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        if "prefetch_excerpt" in _bp_names:
            try:
                _pf_specs = list(prefetch_specs or []) + _bb.prefetch_specs_from_text(task)
                for _nm in ("AUTO-TASK.md", "TASK.md"):
                    _tp = Path(cwd) / _nm
                    if _tp.is_file():
                        _pf_specs += _bb.prefetch_specs_from_text(_tp.read_text(errors="replace"))
                if not _pf_specs:
                    _pf_specs = list(_wr.parse_task_criteria(task)[0])
                _pf_text = _bb.build_prefetch(cwd, _pf_specs)
                if _pf_text:
                    messages[1]["content"] = task + _pf_text
                    log(f"[worker] budget policy prefetch_excerpt: injected {len(_pf_text)} chars from "
                        f"{len(_pf_specs)} spec(s) into the first prompt.")
            except Exception as _pf_err:      # an excerpt is a convenience, never a dispatch breaker
                log(f"[worker] prefetch_excerpt error (ignored): {_pf_err!r}")
        resumed_at_iteration = 0
        resumed_pause_reason = None  # a fresh run was never paused
        _resume_full_messages = None  # no pre-pause history on a fresh run
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = LOG_DIR / f"{ts}.json"

    log(f"[worker] model={model} host={host} cwd={cwd}")
    log(f"[worker] task: {task}")

    # Opt-in live log (--live-log): when set, the native-Ollama-tools path
    # below switches to call_ollama_streaming and appends tagged, colored
    # status lines to this file IN ADDITION to the normal log() output
    # above. When not set, live stays None and every code path below is
    # exactly what it was before this feature existed.
    live = None
    if live_log:
        live = LiveLog(live_log, dispatch_tag or Path(cwd).name, model, host)

    # Populated as the loop runs, written out on success only (see the
    # `if converged:` gate below) via write_dispatch_metrics -- see that
    # function's docstring and DISPATCH_METRICS_PATH above for why this
    # exists. `claude_prep_tokens` is a separate, deliberately distinct
    # figure from everything else here: Claude's own output-token cost
    # (investigation, writing the task spec) for GETTING to this dispatch,
    # captured by the caller via claude-token-cursor.py before/after and
    # passed straight through -- kept apart from sum_completion_tokens
    # (the local model's generation cost) so the two are directly
    # comparable: which side actually burned more tokens on this task.
    # LOUD, because a silent budget change makes two runs look comparable when
    # they are not: the queue watchdog can raise a job's context between attempts,
    # so the number a run actually had must appear in its own log, not only in the
    # metrics file someone may never open.
    if num_ctx_bumps:
        log(f"[worker] CONTEXT BUDGET: running at num_ctx={num_ctx} after "
            f"{num_ctx_bumps} watchdog bump(s). Cost is reported, not penalised -- "
            f"but do NOT compare this run's iteration/token counts against a run "
            f"at a different budget without saying so.")
    else:
        log(f"[worker] CONTEXT BUDGET: num_ctx={num_ctx}, no bumps.")
    # Batch #8 (2026-09-02). A SCORED BAKE-OFF ARM is staged from a verify PROVEN to
    # fail at baseline, so for it the baseline failures ARE the task. Two consequences,
    # one flag: (1) it implies verify_failed_at_baseline -- "proven failing at stage" is
    # a stronger statement than the queue pre-flight's observation, and it must hold even
    # when the arm was fired without that flag; (2) baseline-diagnostic SUBTRACTION is
    # switched off, because subtracting the baseline here hides the only diagnostics that
    # matter and lets an untouched bug read as "no new failures". LOUD, because a scored
    # arm silently graded under the lenient rule produces a number nobody can trust.
    _baseline_is_the_task = bool(verify_failed_at_baseline or scored_arm)
    if scored_arm:
        log("[worker] SCORED ARM: baseline-diagnostic subtraction is OFF and a still-failing "
            "verify can NEVER be accepted -- the baseline failures ARE the task. Do not "
            "compare this run against an unscored dispatch.")
    elif verify_failed_at_baseline:
        log("[worker] BASELINE PROVEN FAILING at enqueue: a still-failing verify cannot show "
            "the fix landed, so it is fed back as work to do, not accepted.")
    _dispatch_metrics.clear()
    _dispatch_metrics.update({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "host": host, "api_style": api_style,
        "configured_num_ctx": num_ctx,
        # How many times the queue's auto-resume watchdog raised this job's
        # context before this run. The owner's framing 2026-09-02: resource cost is
        # REPORTED DATA, never a penalty -- "solved but needed 131072 and 3 bumps"
        # is a deployment fact worth having, and equalising budgets would measure
        # a model we had crippled rather than the one we would deploy. Kept as a
        # first-class metric so a scorer can report cost alongside capability.
        "num_ctx_bumps": num_ctx_bumps,
        # Whether this run was a scored bake-off arm (batch #8). A scored arm is graded
        # under the strict rule above; an unscored dispatch is not. Recorded so the two
        # are never pooled by a scorer reading this file.
        "scored_arm": bool(scored_arm),
        "task_preview": task[:200] + ("..." if len(task) > 200 else ""),
        "task_chars": len(task),
        "claude_prep_tokens": claude_prep_tokens,
        "iterations": 0, "calls": 0,
        "peak_prompt_tokens": 0, "peak_total_tokens": 0, "sum_completion_tokens": 0,
        "verify_passed": None, "status": "running",
    })
    # (path, offset) -> times read. Same-page re-reads are SUCCESSES, so the
    # existing failure-keyed loop detector cannot see them.
    _repeat_reads: dict = {}
    _dispatch_start = datetime.now(timezone.utc)

    # Only manage the local cache when dispatching against this Mac's own
    # Ollama (Unraid's has its own independent GPU + storage, nothing to
    # copy there). Detected by host, not hardcoded to localhost only, so
    # a Mac Studio reached by LAN IP still gets cache management.
    is_local_ollama = api_style == "ollama" and host in ("http://localhost:11434", "http://127.0.0.1:11434")
    if is_local_ollama:
        ensure_model_cached(model)
    # messages[:2] is the [system, task] prefix -- identical on the fresh path
    # (built just above) and on the resume path (_compact_resumed_transcript
    # preserves the head verbatim and only rewrites what follows it). That
    # prefix is exactly what iteration 1 will send, so warming on it is what
    # makes the warmup prime the cache instead of poisoning it.
    _pre_infeasible = None     # DarkbloomColdLoadInfeasible from the load/preflight: pause, do not loop
    try:
        _plan_err = _pr.init_failure_cause() if _pr is not None else None
        if _plan_err:          # ENFORCE only: the plan/key could not be loaded -> resumable pause, fresh plan on relaunch
            raise DarkbloomColdLoadInfeasible(_plan_err)
        ensure_model_ready(host, model, temperature, num_ctx, top_p=top_p, top_k=top_k,
                           api_style=api_style, manual_tools=manual_tools,
                           prime_messages=messages[:2])
    except DarkbloomColdLoadInfeasible as _ci:
        _pre_infeasible = _ci
        log(f"[worker] model not loadable on this memory ({_ci}) -- pausing before the first turn "
            f"(resumable, lane freed; the queue holds it with an INFRA WAIT).")

    converged = False
    final_summary = None  # set by task_complete; falls back to `content` if never set
    # `content` is normally assigned from each model turn inside the loop, but the
    # loop can exit BEFORE its first turn (a resumed run's pre-send projection pause,
    # a chat failure / SIGTERM abort on the first call, an involuntary-pause resume
    # whose ceiling is already spent). --capture-final-as then read
    # `final_summary or content` after the loop and raised UnboundLocalError, turning
    # a clean, resumable pause (exit 3) into a crash (exit 1). Seed it empty.
    content = ""
    completion_claims = 0
    MAX_COMPLETION_CLAIMS = 3  # after this many failed verify-gated claims, accept the
    # model's word rather than looping the claim forever -- same bounded-not-unlimited
    # philosophy as MAX_NUDGES below.
    completion_verify_nudges = 0
    MAX_COMPLETION_VERIFY_NUDGES = 2  # same idea for the silent (no-tool-call) path below
    repeated_failures = {}   # "tool:args" -> consecutive identical failures
    repeated_calls = {}      # "tool:args" -> consecutive identical calls, success or not
    _tool_result_cache = {}  # "tool:args" -> last result for a cacheable read-only call
    _thrash_repeats = {}     # "tool:args" -> times we served this from cache (anti-thrash)
    _write_repeats = {}      # write_thrash_key(path, content) -> successful identical writes to that key (write-side anti-thrash)
    _no_progress = {}        # SHADOW-MODE ONLY (no_progress_track): consecutive net-zero-change iterations. Recorded, never acted on.
    _ctx_nudge_fired = set()  # context-budget thresholds already nudged (fire once each)
    # Frozen-reasoning detector state (see reasoning_repeat_streak). _reasoning_prev is
    # the NORMALISED previous thinking block, not the raw one.
    _reasoning_prev = ""
    _reasoning_streak = 0
    _reasoning_frozen = False
    loop_break_notes = []    # corrective guidance to deliver next turn
    _lguards = _lg.Guards(verify=verify, log=log)   # tool-layer local guards (see worker_local_guards.py)
    nudge_count = 0
    MAX_NUDGES = 3  # hard ceiling regardless of progress -- see below for the
    # condition that governs whether a nudge under that ceiling is actually sent.
    # Added 2026-08-28 (the owner's request): let a model ask for more iterations itself, via the
    # request_more_iterations tool, instead of the only recovery being a human noticing
    # DID-NOT-CONVERGE after the fact. The owner's call: this is a review gate, not an auto-grant --
    # setting this to a non-None reason string cleanly pauses the whole dispatch (both loops),
    # same as running out of iterations, so it's resumable via the exact same `--resume` flow
    # already proven live tonight. See its two setters below (request_more_iterations, and the
    # context-usage threshold check) for what can trigger it.
    paused_for_review = None
    pause_reason_code = None
    pause_meta = None
    if _pre_infeasible is not None:
        paused_for_review = f"transient chat failure: {_pre_infeasible}"
        pause_reason_code = "chat_request_failed"
        pause_meta = {"lane_cause": str(_pre_infeasible)[:300]}
    # Vacuous-verify-pass guard state (added 2026-08-29, Fable GO-with-conditions):
    # a --verify command can pass trivially on a file the model never touched (e.g.
    # `bash -n script.sh` on an untouched script) -- confirmed real, 2026-08-29,
    # a model whose tool calls came through as unparsed text made zero edits and
    # still got VERIFY PASSED. Tracked separately from run_bash success because a
    # model that makes its edits via run_bash (heredoc/sed/git apply) instead of
    # write_file/edit_file is legitimate and must not be penalized the same way as
    # one that made no tool calls that did anything at all -- see the tiered check
    # at verify-evaluation time below.
    _files_modified_count = 0
    _run_bash_success_count = 0
    # Reconstruct both counters from any RESUMED history -- --resume only restores
    # `messages`/`iterations` (see above), every other session flag starts fresh, so
    # without this a dispatch that did real work, paused, and resumed would run
    # verify against a counter that forgot everything before the pause and could
    # wrongly override a legitimate PASS to FAILED. Two message shapes to match,
    # not one: role:"tool" stores the raw result string directly (native/openai
    # paths), but the manual-tools/textual-fallback path stores it as
    # role:"user", content=f"[tool result for {name}]: {result}" (see the
    # tool-result-append `if manual_call_this_turn: ... elif api_style ==
    # "openai": ... else:` branch below) -- searching for the marker anchored to
    # either start-of-string OR right after "]: " covers both without needing to
    # know which path produced any given message.
    _FILE_MOD_RE = re.compile(r'(?:^|\]: )(?:OK: wrote|OK: replaced)')
    _RUN_BASH_OK_RE = re.compile(r'(?:^|\]: )\{"exit_code"')
    # Scan the FULL restored transcript, not the possibly-compacted `messages`: resume
    # compaction (above) may have summarized away the middle where OK:wrote / exit_code
    # markers live, and this reconstruction must still see every pre-pause edit or the
    # vacuous-pass guard would forget real work and false-fail a legitimate completion.
    for _m in (_resume_full_messages if _resume_full_messages is not None else messages):
        _c = str(_m.get("content", ""))
        if _FILE_MOD_RE.search(_c):
            _files_modified_count += 1
        elif _RUN_BASH_OK_RE.search(_c):
            _run_bash_success_count += 1

    # Working-tree snapshot at the ORIGINAL dispatch's start (Fable ruling 2026-08-29,
    # replacing run_bash-success-count as the vacuous-pass guard's primary signal when
    # cwd is a git repo -- see _git_worktree_snapshot's own docstring for why). On
    # --resume this MUST be the true start, not a fresh snapshot taken now: real edits
    # made before a pause plus inaction after resuming would otherwise read as "nothing
    # changed" and false-fail legitimate completed work, the exact counter-amnesia
    # problem already solved above for _files_modified_count/_run_bash_success_count.
    if resume_from:
        _worktree_start_snapshot = saved.get("worktree_start_snapshot")
        if _worktree_start_snapshot is None:
            log("[worker] resumed transcript predates the vacuous-pass worktree-diff "
                "guard -- taking a fresh snapshot now instead of the true dispatch "
                "start (edits made before this resume won't count toward the diff "
                "check this session; the tool-call counters above still cover them).")
            _worktree_start_snapshot = _git_worktree_snapshot(cwd)
    else:
        _worktree_start_snapshot = _git_worktree_snapshot(cwd)

    # Baseline verify-failure signature at the ORIGINAL dispatch's start (added 2026-08-30,
    # same convergence incident as _verify_failure_signature). Captured ONCE, before the
    # model touches anything, so every later verify run can be diffed against it to feed the
    # model only the failures ITS diff caused (see the two completion gates and the end-of-run
    # verify below). Persisted across --resume exactly like _worktree_start_snapshot and for
    # the identical counter-amnesia reason: re-running the verify at resume time would capture
    # a baseline that already contains the model's pre-pause edits, so pre-pause work would
    # wrongly count as pre-existing (masking a real regression) -- the baseline must be the
    # TRUE start. Stored as a sorted list in JSON (sets aren't JSON-serializable); restored to
    # a set. None means "no baseline available" -> _verify_delta_feedback treats every failure
    # as new, i.e. exactly the old pre-baseline behavior. Only meaningful for coding tasks with
    # a --verify; a passing baseline yields the empty set (no pre-existing failures to subtract).
    _baseline_verify_sig = None
    if verify and task_kind == "coding":
        if resume_from:
            _saved_bl = saved.get("baseline_verify_sig")
            if _saved_bl is not None:
                _baseline_verify_sig = set(_saved_bl)
            else:
                log("[worker] resumed transcript predates the baseline-verify-delta guard -- "
                    "no start-of-dispatch baseline available, so this session's verify feedback "
                    "falls back to the full raw output (pre-existing failures can't be subtracted "
                    "from a baseline that was never recorded).")
        else:
            _bl_ok, _bl_out = _quick_verify(verify, cwd)
            if _bl_ok:
                _baseline_verify_sig = set()
                log("[worker] baseline verify PASSED at dispatch start -- any later failure is "
                    "attributable to the model's work.")
            else:
                _baseline_verify_sig = _verify_failure_signature(_bl_out)
                log(f"[worker] baseline verify FAILED at dispatch start with "
                    f"{len(_baseline_verify_sig)} pre-existing diagnostic(s) -- these will be "
                    f"subtracted from later verify runs so the model is only asked to fix what its "
                    f"own diff introduces. (A large count here usually means the worktree wasn't "
                    f"fully provisioned, e.g. a missing codegen/`prisma generate` step.)")

    empty_answer_nudge_sent = False
    _think_cap_streak = 0          # consecutive truncated-in-thinking turns (see THINK_CAP_*)
    _cap_cut_streak = 0            # consecutive output-cap prose cuts (see OUTPUT_CAP_CUT_MAX)
    _cap_cut_iters = []            # iterations that were cut off (see PROSE_LOOP_WINDOW)
    _wall_budget = wall_budget_s(task) if task_kind == "coding" else None
    _wall_t0 = time.monotonic()
    _cap_cut_recover = False       # the NEXT call samples off the loop (OUTPUT_CAP_RECOVERY_*)
    _think_cap_budget = None       # num_predict override for the NEXT call only
    tool_called_since_last_nudge = True  # starts True so the first nudge is
    # always allowed regardless of history, matching the original "exactly one
    # nudge" design's unconditional first attempt.
    # For coding tasks: specifically write_file/edit_file, not any tool --
    # confirmed live 2026-08-22 (qwen3.8:27b-q8_0, resell-tracker-photo-upload): a
    # model can do 18 iterations of pure read_file/run_bash exploration, narrate a
    # full implementation as prose on iteration 19, get cut off mid-sentence with no
    # tool call, and stop -- because the old `any_tool_called` flag was set True by
    # the very first read_file/run_bash call in iteration 1, permanently disarming
    # the corrective-nudge safeguard below for the rest of the run. Tracking mutation
    # calls specifically (not any tool call) is what the nudge actually needs to mean
    # "the model has made real progress," not "the model has done anything at all."
    #
    # task_kind gates what "real progress" means, added 2026-08-28: confirmed live
    # (qwen3:8b, twice, identically) that this write_file/edit_file-only definition
    # is a coding-task assumption that doesn't fit research tasks at all -- a research
    # dispatch's deliverable IS the final text response, there's no file to save, so
    # this condition was structurally impossible to satisfy and every research
    # dispatch got at least one guaranteed false-positive nudge, even after doing
    # real web_fetch-verified work and correctly concluding. In both traced cases the
    # model complied with the nudge's suggested action literally -- calling write_file
    # to save its (already-written, not yet source-verified) answer to a file it was
    # never asked to produce -- actively steering it toward the wrong action rather
    # than just failing to help. For task_kind="research", ANY tool call at all counts
    # as real progress (there's no equivalent "narrated but never saved" failure mode
    # to guard against when the deliverable is text, not a file).
    any_mutation_called = False
    if resume_from and (_files_modified_count > 0 or _run_bash_success_count > 0):
        # Fable finding 2026-08-29: --resume only restores messages/iterations (see
        # _save_transcript's own docstring), so any_mutation_called reset to False on
        # every resume regardless of real pre-pause progress -- confirmed live, this
        # produced a false "you have not made real progress on the task yet" nudge
        # after a resume that already had 2 successful edits, which then drove the
        # model into a no-op re-edit loop. _files_modified_count/_run_bash_success_count
        # are already correctly reconstructed from message history above; reuse that
        # instead of adding a third, separately-fragile reconstruction path.
        any_mutation_called = True
    # Confirmed live 2026-08-28 (llama3.1:8b, and qwen3:8b earlier tonight):
    # a research dispatch can converge on a confident final answer with
    # specific fabricated numbers after every single web_fetch call failed,
    # directly against an explicit "don't state facts from a snippet alone"
    # instruction. Track whether ANY web_fetch this dispatch has ever
    # actually succeeded so the convergence check below can catch this
    # before accepting the answer -- see its use near "no tool calls in
    # response" below. fabrication_nudge_sent bounds it to exactly one nudge
    # (same bounded-not-looping philosophy as nudge_count/MAX_NUDGES above).
    web_fetch_succeeded = False
    web_fetch_success_count = 0
    # Count genuine LOCAL-source verification (read_file/list_files/read-only run_bash
    # like grep/cat/find) so the end-of-run unverified-provenance warning can tell two
    # cases apart, added 2026-09-06 (false-signal fix, job 8d690b764cec): a research
    # dispatch whose real sources are local repo files correctly makes ZERO web_fetch
    # calls, and stamping "claims of verification can't be trusted" on it is a FALSE
    # signal -- it DID verify, just not over the web. The warning is reserved for a
    # research answer that shows NO verification of ANY kind (zero web_fetch AND zero
    # local reads). Like web_fetch_succeeded above, this is NOT reconstructed on
    # --resume (matching that flag's existing semantics), so both share the same
    # resume blind spot rather than introducing a new asymmetry.
    local_read_count = 0
    # Confirmed live 2026-08-28 (Electrify America research): the grounding checks below used
    # to scrape ALL tool-role messages for their grounding source, which silently included
    # web_search snippets alongside real web_fetch content -- exactly the "snippet, not a
    # confirmed source" distinction the system prompt itself warns about. A model cited a real
    # number ("328 stations") that only ever appeared in an unrelated site's SEARCH SNIPPET,
    # attributed to a URL that actually 404'd, and the grounding check passed it because the
    # snippet text was in the same pool as real fetches. Track only genuine successful
    # web_fetch results here, at the one place that already knows which tool call this is, so
    # grounding can never be satisfied by a snippet again.
    real_fetched_texts = []
    min_fetches_nudge_count = 0
    MAX_MIN_FETCH_NUDGES = 3  # bounded, not unlimited -- see the nudge site below for why
    fabrication_nudge_sent = False
    # 2026-08-22 (devstral:24b, resell-tracker-photo-upload): the original single
    # nudge worked -- it produced a real tool call on the very next turn -- but the
    # model then relapsed into narration two iterations later with no nudges left to
    # correct it. That's meaningfully different from opencode's infinite self-nudge
    # bug (which re-injected the same generic message forever regardless of whether
    # the model ever responded to it): here each nudge was demonstrably producing
    # real forward progress, just not durably. The `tool_called_since_last_nudge`
    # gate is what preserves the original safety property -- a model that ignores a
    # nudge outright (zero tool calls afterward) does NOT get another one, so a
    # truly stuck model still stops after one unproductive nudge, same as before.
    # Only a model demonstrating it's actually listening gets the extra budget, and
    # even that is hard-capped at MAX_NUDGES so this can never become unbounded.
    # --resume: iteration numbering continues from where the saved transcript
    # left off rather than restarting at 1 -- the file already had
    # resumed_at_iteration iterations before this session, so max_iters here
    # means "how many MORE iterations are allowed this session", not a reset
    # of the total count. On a fresh run resumed_at_iteration is 0 and this
    # is exactly the original range(1, max_iters + 1).
    total_iters = resume_total_iters(resumed_at_iteration, max_iters, resumed_pause_reason)
    # CONTINUATION BUDGET (--extra-iterations): the queue marks an authoring job that is still making
    # headway 'nonconvergence_progressing' with continuation_allowance.extra_iterations; the driver
    # passes that number here for ONE round. Additive to whatever ceiling the resume rules above
    # produced (a granting pause, a plain resume, or an involuntary pause restoring the original cap).
    total_iters, _extra_applied = apply_extra_iterations(total_iters, extra_iterations)
    if _extra_applied:
        log(f"[worker] CONTINUATION BUDGET: --extra-iterations {_extra_applied} -> iteration ceiling "
            f"{total_iters} (this round only).")
        _dispatch_metrics["extra_iterations"] = _extra_applied
    # An involuntary-pause resume can hand back a ceiling <= resumed_at_iteration
    # (e.g. a resume-of-a-resume already at the cap) -- range() is then empty and
    # the loop body never runs, leaving `i` unbound at the final _save_transcript
    # below (real incident: job b71d329d9367, 2026-09-21, UnboundLocalError).
    # Seed it to the last completed iteration so that save is a same-state no-op
    # instead of a crash.
    i = resumed_at_iteration
    # worker_robust state (Phase 3). The iteration counter is a manual while-loop (was
    # `for i in range(...)`) ONLY so a format-error requery can be FREE: `i -= 1; continue`
    # re-runs the same iteration number instead of charging the model an attempt.
    _rb = _prof.get("robust") or {}
    _fmt_requery = _wr.FormatRequery(int(_rb.get("format_requery_max", 3)))
    _loop_det = _wr.LoopDetector(_wr.loop_config(_rb))
    _rev_bounds = _wr.review_bounds_active(task_kind, capture_final_as)
    _rep_guard = _wr.RepeatCallGuard()
    # Sampling escalation ladder: default OFF (model_profiles.yaml sampling_escalation.arm).
    # Per-job arm with no queue change: a `Sampling-arm: ladder|off` line in the task text sets this
    # worker process's MODEL_SAMPLING_ARM (one process per job), which build_request_fields reads.
    _sa_m = re.search(r"(?im)^[ \t]*Sampling-arm:[ \t]*(ladder|off)[ \t]*$", task or "")
    if _sa_m and not os.environ.get("MODEL_SAMPLING_ARM"):
        os.environ["MODEL_SAMPLING_ARM"] = _sa_m.group(1).lower()
        log(f"[worker] SAMPLING ARM {_sa_m.group(1).lower()} (from the task's Sampling-arm line; A/B arm, unvalidated on Qwen3.6).")
    _ladder = _wr.SamplingLadder(
        enabled=_mp.sampling_ladder_enabled(role), seed_base=int(time.time()) % 100000)
    if _ladder.enabled:
        log("[worker] SAMPLING LADDER enabled (A/B arm): abort1 -> temp>=0.8/rep 1.2/new seed, abort2 -> "
            "presence 1.0 (+thinking off under the hybrid arm), abort3 -> sampling_ladder_exhausted; "
            "2 aborts on the same failing check -> spec_defect_repeat_abort.")
    _ladder_exit = None
    _rev_abort = None             # (named reason, detail) once a review bound trips; ends the run at the next turn top
    _fan_guard = _wr.TurnFanoutGuard(cap=MAX_TOOL_CALLS_PER_TURN)   # ALL task kinds (job c8f4f6ed95c1)
    _fan_abort = None             # (named reason, detail) once the fan-out / past-EOF guard says stop
    _stop_gate_fails = 0
    _STOP_GATE_MAX = int(_rb.get("stop_gate_max", 3))
    _stop_gate_exhausted = False
    _reasoning_budget_chars = int(_rb.get("reasoning_budget_chars", 24000) or 0)
    _runaway_streak = 0
    _loop_exhaust_exit = _wr.loop_exhaust_exit_enabled(_rb)   # default OFF: exhausted loop recovery pauses (legacy)
    _bash_only = _bash_only_mode
    _tool_extractor = bool(_rb.get("tool_extractor"))
    _sg_files, _sg_lits_raw = _wr.parse_task_criteria(task)
    # EDIT_CHECKPOINT=1: arm the passing-verify snapshot (see _edit_checkpoint_on_pass). Reset per run.
    _EDIT_CKPT.update(cwd=str(cwd), dest=(Path(str(log_path)).with_suffix(".checkpoint.patch")
                                          if _edit_checkpoint_enabled() else None),
                      files=tuple(_sg_files or ()), lits=tuple(_sg_lits_raw or ()),
                      info=None, passes=0, verify=verify)
    if _wl is not None:     # convergence levers (default OFF): arm per-run state; PROTECT_HARNESS only for a sealed coding job
        _wl.arm_run(cwd, task, task_kind, verify, log=log)
    _sg_entry = _wr.parse_entry_point(task)
    _next_turn_think = None   # one-turn override of `think` (runaway-reasoning / think-cap recovery)
    _REASONING_BUDGET[0] = _reasoning_budget_chars
    _SYNTAX_GUARD_ON[0] = _wr.syntax_guard_enabled(_rb)
    _PROSE_BUDGET_TOKENS = int(_rb.get("prose_budget_tokens", 12000) or 0)
    # Every task kind is bounded (job f268674ef763: a read-only research turn streamed >1600 lines
    # of no-tool-call content for 4+ min because this was coding-only). Coding keeps the generous
    # prose budget (large legit files); a research/review/other answer gets the tighter ANSWER budget.
    _PROSE_BUDGET_TOKENS = (_PROSE_BUDGET_TOKENS if task_kind == "coding"
                            else int(_rb.get("answer_budget_tokens", 6000) or 0))
    _PROSE_BUDGET[0] = _PROSE_BUDGET_TOKENS * 4
    _mask_on = _wr.obs_mask_enabled(_rb)
    _mask_cfg = _wr.obs_mask_config(_rb)
    _mask_state = {}
    _bp = None                    # worker_budget.BudgetState; None (the default) = every policy hook below is skipped
    if _bp_names and _bb is not None:
        _bp = _bb.BudgetState(
            _bp_names, total_iters,
            refine=("# REFINE TASK" in task or "refine" in str(dispatch_tag or cwd.name).lower()),
            sig_fn=_verify_failure_signature)
        _bp.set_cwd(cwd)
        _dispatch_metrics.update(_bp.metrics())
        log(f"[worker] BUDGET POLICY active ({_bp_source}): {', '.join(sorted(_bp_names))} "
            f"(base cap {total_iters}; ceiling {int(total_iters * _bb.EXTEND_CEILING_FACTOR)}).")

    def _bp_ext():
        # loop-condition fallback: an iteration that ended via `continue` (silent-stop nudges) skips
        # the end-of-iteration block, so the cap-time extension decision is also made here. Idempotent.
        nonlocal total_iters
        if _bp is None or paused_for_review or converged:
            return False
        try:
            _x, _note = _bp.try_extend(i, total_iters)
        except Exception as _e:
            log(f"[worker] budget extension error (ignored): {_e!r}")
            return False
        if _x:
            total_iters += _x
            messages.append({"role": "user", "content": _note})
            _dispatch_metrics.update(_bp.metrics())
            log(f"[worker] BUDGET POLICY progress_extend: +{_x} iterations -> ceiling {total_iters}.")
            return True
        _dispatch_metrics.update(_bp.metrics())
        return False
    _fixed_lane_reason = None     # fixed_lane_{exhausted,apply_failed,transport_error} when the lane ran and failed
    if mode in ("auto", "fixed"):
        _fl = _run_fixed_lane_seam(
            mode=mode, model=model, host=host, cwd=cwd, task=task, verify=verify, task_kind=task_kind,
            api_style=api_style, resume_from=resume_from, bash_only=_bash_only_mode,
            manual_tools=manual_tools, targets=(fixed_targets or _sg_files), rb=_rb, messages=messages)
        if _fl["converged"]:
            converged = True
            final_summary = _fl["summary"]
            _files_modified_count = max(_files_modified_count, _fl["files_changed"])
            total_iters = i      # nothing left for the open loop; the normal verify/gate path runs below
        _fixed_lane_reason = _fl["reason"]
    while (i < total_iters or _bp_ext()) and _pre_infeasible is None:
        i += 1
        log(f"[worker] --- iteration {i}/{total_iters} ---")
        if live is not None:
            live.iteration(i, total_iters)
        _dispatch_metrics["iterations"] = i
        if _bp is not None:
            _bp.begin_turn(i)

        if _rev_abort:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: bounded review -- {_rev_abort[1]} "
                f"-- {_rev_abort[0]}. Stopping; the captured answer is a REVIEW CAPPED marker, "
                f"not a verdict.")
            _dispatch_metrics["early_abort"] = _rev_abort[0]
            break

        if _wall_budget and time.monotonic() - _wall_t0 > _wall_budget:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: WALL BUDGET spent "
                f"({int(time.monotonic() - _wall_t0)}s > {int(_wall_budget)}s for a "
                f"{harness_task_kind(task) or 'capped'} run) -- stopping so the driver can "
                f"re-plan instead of grinding for hours.")
            _dispatch_metrics["early_abort"] = "wall_budget"
            break

        # MONOLOGUE (worker_robust.LoopDetector.monologue_verdict): 3 consecutive assistant turns with
        # no tool call that the run nevertheless continued past (nudges, verify-gate) -- the model is
        # talking, not working. Coding runs only: a research run is legitimately nudged for fetches.
        if task_kind == "coding" and not paused_for_review:
            try:
                _mono = _loop_det.monologue_verdict()
            except Exception as _ld_err:
                _mono = None
                log(f"[worker] loop-detector monologue error (ignored): {_ld_err!r}")
            if _mono:
                log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_mono[2]} -- "
                    f"{_wr.REASON_MONOLOGUE}.")
                _dispatch_metrics["loop_detector"] = {"kind": "monologue", "action": "stop", "iteration": i}
                _dispatch_metrics["early_abort"] = _wr.REASON_MONOLOGUE
                break

        # EARLY NON-CONVERGENCE ABORT (2026-09-10). A goalless run -- typically a
        # coding dispatch enqueued with no --verify (now gated at enqueue in
        # ollama-queue.py; kept here as defence in depth) -- thrashes: it re-issues
        # the same reads, the anti-thrash cache serves them, and it grinds to
        # max_iters with a ZERO diff. Job d31d96d23b29 (shipped-flip) did exactly
        # that: 29 cache-serves, 55/55 iterations, empty diff. Once the thrash is
        # unambiguous (past an iteration floor + many accumulated cache-serves) AND
        # the working tree is STILL byte-for-byte unchanged since dispatch start,
        # bail instead of burning the rest of the budget. The tree-unchanged check
        # is LAST so its git snapshot only runs once the cheap thrash signal has
        # already tripped; the conjunction with "unchanged tree" means this can
        # never fire on a run that has actually edited anything. Thresholds were
        # tightened 2026-09-13 (10->5 / 15->10, floor 12->8): loop-detect's 3x
        # nudge fires first, and a model that ignores it and keeps re-issuing the
        # same call on a still-unchanged tree is cut ~5 iters sooner (arm A of the
        # s2 bake-off spun git-status run_bash 3x past the nudge, wasting GPU to ~x10).
        if (i > 8 and _worktree_start_snapshot is not None
                and (max(_thrash_repeats.values(), default=0) >= 5
                     or sum(_thrash_repeats.values()) >= 10)
                and _git_worktree_snapshot(cwd) == _worktree_start_snapshot):
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: "
                f"{sum(_thrash_repeats.values())} anti-thrash cache-serves "
                f"(top signature x{max(_thrash_repeats.values(), default=0)}) and the "
                f"working tree is STILL unchanged since dispatch start -- non-productive "
                f"thrash, not converging. Stopping to save GPU rather than grinding to "
                f"max_iters. (A no-verify dispatch has no goal signal to converge to; "
                f"attach a --verify.)")
            _dispatch_metrics["early_abort"] = "thrash_zero_diff"
            break

        # WRITE-SIDE THRASH ABORT (see write_thrash_key/write_thrash_abort above): the
        # read-only branch can never fire for a run that keeps writing, so 3+ identical
        # writes to one path -- or any scaffold-sentinel re-emission, which collapses to
        # ONE key per path regardless of surrounding bytes -- is non-advancing by
        # construction and stops here on the next turn instead of grinding to max_iters.
        _wt_abort, _wt_msg = write_thrash_abort(_write_repeats)
        if _wt_abort:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_wt_msg} -- "
                f"re-emitting the same bytes is not converging; stopping to save GPU rather than grinding to max_iters.")
            _dispatch_metrics["early_abort"] = "write_thrash"
            break

        # SHADOW-MODE no-net-progress sampling (no_progress_track). Deliberately
        # placed AFTER both early-abort blocks so an aborting iteration never pays
        # for a measurement nobody will read, and so it is self-evidently incapable
        # of pre-empting either of them.
        #
        # It cannot change what this loop does: it only mutates _no_progress and
        # writes two _dispatch_metrics keys, and the whole thing is swallowed --
        # telemetry that can break a dispatch is worse than no telemetry.
        #
        # Cost, measured rather than assumed: _git_worktree_snapshot is 25-38ms on
        # this machine's real repos (resell-tracker 37.6, machine-config 25.2),
        # against multi-second generation calls -- under 1% per iteration. The floor
        # keeps short runs from paying at all. Noted because the read-side abort
        # above deliberately orders its snapshot LAST for this same reason.
        if i > NO_PROGRESS_SAMPLE_FLOOR:
            try:
                no_progress_track(_no_progress, _git_worktree_snapshot(cwd))
                _dispatch_metrics["no_progress_streak_max"] = _no_progress.get("max_streak", 0)
                _dispatch_metrics["no_progress_samples"] = _no_progress.get("samples", 0)
            except Exception:
                pass

        # PRE-SEND ACCUMULATION GUARD. With read_file capped, run_bash at
        # 4000+4000 and web_fetch at 5000, no SINGLE tool result can blow the
        # window any more -- the residual risk is the transcript growing past
        # it across many iterations. The existing context check runs on the
        # usage the API reports AFTER a call, which cannot help when the
        # request itself is already over: that request fails or silently
        # truncates, and the pause lands too late.
        #
        # So project the prompt size BEFORE sending (the same bytes/4 heuristic
        # used at dispatch time) and, if it is about to exceed the window, take
        # the EXISTING context_threshold pause path -- resumable with a bigger
        # --num-ctx -- rather than sending a doomed request. Warn-and-pause,
        # never a hard refusal: the run is recoverable, and pausing keeps the
        # transcript intact for the resume.
        _send_messages = messages
        if _mask_on:
            try:
                _send_messages, _ms = _wr.mask_observations(messages, _mask_cfg, _mask_state, verify)
                if _ms["masked"]:
                    _dispatch_metrics["obs_masked"] = _ms["masked"]
                    _dispatch_metrics["obs_chars_saved"] = _ms["chars_saved"]
                    if _mask_state.get("logged") != _ms["boundary"]:
                        _mask_state["logged"] = _ms["boundary"]
                        log(f"[worker] observation masking: {_ms['masked']} old tool output(s) elided "
                            f"({_ms['chars_saved']} chars) from the prompt (transcript unchanged).")
            except Exception as _mk_err:      # a context optimisation must never break a dispatch
                log(f"[worker] observation masking error (ignored): {_mk_err!r}")
                _send_messages = messages
        if _bp is not None:
            try:
                _send_messages = _bp.mask_view(_send_messages)
            except Exception as _bm_err:
                log(f"[worker] budget stale-read masking error (ignored): {_bm_err!r}")
        if num_ctx and not paused_for_review:
            _projected = sum(len(str(_m.get("content") or "")) for _m in _send_messages) // 4
            if _projected >= num_ctx * PRESEND_CONTEXT_LIMIT:
                paused_for_review = (
                    f"projected prompt ~{_projected} tokens vs {num_ctx} context "
                    f"({_projected / num_ctx:.0%}) -- pausing BEFORE sending a request "
                    f"that would not fit")
                pause_reason_code = "context_threshold"
                pause_meta = {"tokens_used": _projected, "num_ctx": num_ctx,
                              "detected": "pre-send projection"}
                log(f"[worker] PAUSED FOR REVIEW: {paused_for_review}. Resume with "
                    f"--resume {log_path} --num-ctx <bigger>.")
                break

        _call_started = time.monotonic()
        # One recovery turn after a truncated-in-thinking reply gets a larger budget
        # (think_cap_recovery_budget); every other turn uses the normal cap.
        _turn_max_tokens = _think_cap_budget or max_tokens
        _think_cap_budget = None
        _reasoning_loop_retries = 0
        _eff_temperature = temperature
        _turn_repeat_penalty = repeat_penalty
        _l4_sticky = _wl is not None and _wl.l4_sticky()    # PROSE_STICKY_NOTHINK (default OFF): recovery profile every turn after the first abort
        if _cap_cut_recover or _l4_sticky:
            # One recovery turn after an output-cap cut: off the near-greedy loop.
            _eff_temperature = max(temperature or 0.0, REASONING_LOOP_RETRY_TEMPERATURE)
            _turn_repeat_penalty = max(repeat_penalty or 1.0, OUTPUT_CAP_RECOVERY_REPEAT_PENALTY)
            if _cap_cut_recover:
                _cap_cut_recover = False
                log(f"[worker] output-cap recovery turn: temperature={_eff_temperature}, "
                    f"repeat_penalty={_turn_repeat_penalty} (this turn only).")
            else:
                log(f"[worker] L4 sticky recovery profile: temperature={_eff_temperature}, "
                    f"repeat_penalty={_turn_repeat_penalty}, thinking off (every turn after the first prose runaway).")
        _outer_break = False

        def _view():
            # the prompt actually sent: old bulky tool outputs masked (deterministic, see mask_observations)
            base = messages
            if _mask_on:
                try:
                    base = _wr.mask_observations(messages, _mask_cfg, _mask_state, verify)[0]
                except Exception:
                    base = messages
            if _bp is not None:
                try:
                    base = _bp.mask_view(base)
                except Exception:
                    pass
            return base
        _turn_think = think if _next_turn_think is None else _next_turn_think
        if _l4_sticky:
            _turn_think = False
        _next_turn_think = None
        _turn_sstep, _turn_seed = _ladder.take()
        if _turn_sstep:
            _dispatch_metrics["sampling_steps"] = _dispatch_metrics.get("sampling_steps", 0) + 1
            log(f"[worker] sampling ladder step {_turn_sstep} for this turn only (fresh seed {_turn_seed}).")
        while True:
            try:
                if live is not None and api_style == "ollama" and not manual_tools:
                    # Streaming path, native Ollama tools only (see
                    # call_ollama_streaming's docstring for scope). Returns the same
                    # normalized {"message": {...}, "usage": {...}} shape as
                    # call_ollama, so nothing downstream changes.
                    resp = call_ollama_streaming(host, model, _view(), _eff_temperature, num_ctx,
                                                 timeout=chat_timeout, tools=not manual_tools,
                                                 top_p=top_p, top_k=top_k, live=live,
                                                 max_tokens=_turn_max_tokens, repeat_penalty=_turn_repeat_penalty,
                                                 think=_turn_think, role=role,
                                                 sampling_step=_turn_sstep, seed=_turn_seed)
                elif live is not None and api_style == "openai" and not manual_tools:
                    # Darkbloom / OpenAI-style streaming lane (added 2026-10-01). Same
                    # normalized return shape; OpenAIStreamUnavailable means the stream
                    # never started (or died before the first token), in which case the
                    # blocking path below runs the identical turn -- --live-log must never
                    # be able to break a dispatch that would otherwise have worked.
                    try:
                        resp = call_openai_streaming(host, model, _view(), _eff_temperature, num_ctx,
                                                     timeout=chat_timeout, tools=not manual_tools,
                                                     top_p=top_p, top_k=top_k, live=live,
                                                     max_tokens=_turn_max_tokens,
                                                     repeat_penalty=_turn_repeat_penalty,
                                                     preserve_reasoning=preserve_reasoning,
                                                     think=_turn_think, role=role,
                                                 sampling_step=_turn_sstep, seed=_turn_seed)
                    except OpenAIStreamUnavailable as _sse_err:
                        log(f"[worker] live-log streaming unavailable on the OpenAI lane "
                            f"({_sse_err}) -- falling back to the non-streaming request for "
                            f"this turn.")
                        resp = call_ollama(host, model, _view(), _eff_temperature, num_ctx,
                                           timeout=chat_timeout, tools=not manual_tools,
                                           top_p=top_p, top_k=top_k, api_style=api_style,
                                           max_tokens=_turn_max_tokens,
                                           repeat_penalty=_turn_repeat_penalty, think=_turn_think,
                                           preserve_reasoning=preserve_reasoning, role=role,
                                           sampling_step=_turn_sstep, seed=_turn_seed)
                else:
                    resp = call_ollama(host, model, _view(), _eff_temperature, num_ctx, timeout=chat_timeout,
                                        tools=not manual_tools, top_p=top_p, top_k=top_k, api_style=api_style,
                                        max_tokens=_turn_max_tokens, repeat_penalty=_turn_repeat_penalty, think=_turn_think,
                                        preserve_reasoning=preserve_reasoning, role=role,
                                        sampling_step=_turn_sstep, seed=_turn_seed)
                _LANE_LAST_CAUSE[0] = None     # this turn succeeded: no stale cause
                break
            except ChatAbortedForReasoningRunaway as rr_err:
                # worker_robust runaway-reasoning guard (output_cap_loop root cause): the turn
                # spent the whole thinking budget without a tool call. Re-prompt ONCE with
                # thinking OFF for this turn (profile non-thinking mode) instead of waiting for
                # the 32768-token cap; a second runaway in a row ends the run with a named reason.
                _runaway_streak += 1
                _dispatch_metrics["reasoning_runaways"] = _dispatch_metrics.get("reasoning_runaways", 0) + 1
                _rr_chars = rr_err.args[0] if rr_err.args else 0
                _persist_reasoning_abort(log_path, i, "runaway", rr_err, log, _dispatch_metrics)
                _lad = _ladder.on_abort("reasoning_runaway")
                if _lad[0] == "exit":
                    log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: sampling ladder -- {_lad[1]} "
                        f"after a reasoning runaway ({_rr_chars} chars).")
                    _dispatch_metrics["early_abort"] = _lad[1]
                    _outer_break = True
                    break
                _turn_sstep, _turn_seed = _ladder.take()   # the retry below IS the next turn
                if _turn_sstep:
                    log(f"[worker] sampling ladder step {_turn_sstep} on the runaway retry (fresh seed {_turn_seed}).")
                if _turn_think is False or _runaway_streak > 2:
                    log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: REASONING RUNAWAY again "
                        f"({_rr_chars} chars, thinking already disabled for this turn or "
                        f"{_runaway_streak} in a row) -- stopping instead of looping.")
                    _dispatch_metrics["early_abort"] = _wr.REASON_REASONING
                    _outer_break = True
                    break
                _turn_think = False
                messages.append({"role": "user", "content": _wr.reasoning_runaway_message(_rr_chars)})
                log(f"[worker] REASONING RUNAWAY at iteration {i}/{total_iters}: {_rr_chars} chars of "
                    f"thinking, no tool call -- retrying this turn with thinking DISABLED "
                    f"(runaway {_runaway_streak}).")
                _call_started = time.monotonic()
                continue
            except ChatAbortedForProseRunaway as pr_abort:
                # Stream guard (smoke 6cf6fb3afca4): a coding turn ran past the no-tool-call
                # prose budget or degenerated into repetition. Hand it to the EXISTING
                # cut-off handling below as a synthetic length-finished turn (persist ->
                # compact -> nudge -> prose_loop accounting); the partial text is the turn.
                _dispatch_metrics["prose_runaway_aborts"] = _dispatch_metrics.get("prose_runaway_aborts", 0) + 1
                resp = {"message": {"role": "assistant", "content": pr_abort.content},
                        "done_reason": "length", "streamed": pr_abort.streamed,
                        "usage": {"prompt_tokens": 0, "total_tokens": 0,
                                  "completion_tokens": max(1, (len(pr_abort.content) + len(pr_abort.thinking)) // 4)},
                        "_prose_runaway": pr_abort.reason, "_raw_thinking": pr_abort.thinking}
                break
            except ChatAbortedForReasoningLoop as loop_err:
                # The owner 2026-09-20, 4 live occurrences in one night (bece829d8005,
                # d08795db97ee, 0971afaccfa9, ...): a single turn stuck re-deriving the
                # same reasoning at temperature=0. A same-temperature retry of a frozen
                # decode reproduces itself deterministically, so bump temperature before
                # retrying -- this can sometimes actually unstick the model, not just
                # detect it faster. REASONING_LOOP_MAX_RETRIES bounds it: this is a
                # faster, cheaper failure than the old "burn hours/max_iters", not a
                # promise the model will converge.
                _reasoning_loop_retries += 1
                _dispatch_metrics["reasoning_loop_aborts"] = _dispatch_metrics.get("reasoning_loop_aborts", 0) + 1
                _persist_reasoning_abort(log_path, i, "loop", loop_err, log, _dispatch_metrics)
                if _reasoning_loop_retries > REASONING_LOOP_MAX_RETRIES:
                    # Retries exhausted. Do NOT park the job awaiting a human (A/B 2026-10-12:
                    # 3 of 3 REASONING_PARSER=qwen3 runs ended paused/exit 3 here). Take the same
                    # recovery rung a reasoning runaway takes: nudge + this turn with thinking OFF.
                    _runaway_streak += 1
                    _lc = len(getattr(loop_err, "thinking", "") or "") or 0
                    if _turn_think is False or _runaway_streak > 2:
                        if not _loop_exhaust_exit:
                            log(f"[worker] REASONING LOOP at iteration {i}/{total_iters}: {loop_err}. "
                                f"{_reasoning_loop_retries} attempt(s) exhausted and thinking-off recovery "
                                f"already used ({_runaway_streak} in a row) -- pausing as resumable rather "
                                f"than retrying forever.")
                        if _loop_exhaust_exit:
                            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: reasoning-loop recovery "
                                f"exhausted -- {_wr.REASON_LOOP_EXHAUSTED} (scheduler should re-spec, not resume).")
                            _dispatch_metrics["early_abort"] = _wr.REASON_LOOP_EXHAUSTED
                            _outer_break = True
                            break
                        paused_for_review = f"reasoning loop, retries exhausted: {loop_err}"
                        pause_reason_code = "reasoning_loop"
                        pause_meta = {"retries": _reasoning_loop_retries}
                        _outer_break = True
                        break
                    _turn_think = False
                    messages.append({"role": "user", "content": _wr.reasoning_runaway_message(_lc)})
                    _dispatch_metrics["reasoning_loop_thinkoff"] = _dispatch_metrics.get("reasoning_loop_thinkoff", 0) + 1
                    log(f"[worker] REASONING LOOP at iteration {i}/{total_iters}: {loop_err}. "
                        f"{_reasoning_loop_retries} attempt(s) exhausted -- retrying this turn with "
                        f"thinking DISABLED (recovery {_runaway_streak}).")
                    _call_started = time.monotonic()
                    continue
                _eff_temperature = max(_eff_temperature, REASONING_LOOP_RETRY_TEMPERATURE)
                log(f"[worker] REASONING LOOP at iteration {i}/{total_iters}: {loop_err}. Retrying "
                    f"this turn at temperature={_eff_temperature} -- attempt "
                    f"{_reasoning_loop_retries}/{REASONING_LOOP_MAX_RETRIES}.")
                _call_started = time.monotonic()
                continue
            except ChatAbortedForPause as pause_err:
                # Bug #1 (2026-09-18): an external SIGTERM pause landed mid-generation and
                # the streaming loop aborted the in-flight request to free the lane. Treat
                # exactly like the between-iterations SIGTERM pause (external_sigterm) so a
                # gate preemption auto-resumes -- NOT a failure and NOT a transient chat
                # error. The transcript through the last completed iteration is already on
                # disk from the previous iteration's incremental save.
                log(f"[worker] CHAT ABORTED FOR PAUSE at iteration {i}/{total_iters}: {pause_err}")
                paused_for_review = "external pause (SIGTERM) from queue daemon (mid-generation)"
                pause_reason_code = "external_sigterm"
                pause_meta = {}
                _outer_break = True
                break
            except RuntimeError as chat_err:
                if api_style == "openai" and not manual_tools and _is_422_gen_failed(chat_err):
                    log(f"[worker] 422 recovery: server generation failure persists after the normal "
                        f"retries at iteration {i}/{total_iters} -- running the recovery ladder.")
                    _rec_resp = _recover_422(
                        lambda _m, **_kw: call_ollama(
                            host, model, _m, _eff_temperature, num_ctx, timeout=chat_timeout,
                            tools=True, top_p=top_p, top_k=top_k, api_style=api_style,
                            max_tokens=_turn_max_tokens, repeat_penalty=_turn_repeat_penalty,
                            think=_turn_think, preserve_reasoning=preserve_reasoning, role=role,
                            max_attempts=1, **_kw),
                        _view(), _dispatch_metrics, iteration=i)
                    if _rec_resp is not None:
                        resp = _rec_resp
                        _LANE_LAST_CAUSE[0] = None
                        break
                # A chat request that exhausted all its retries (a transient Ollama HTTP
                # 500 / template-parse "XML syntax error" / network drop mid-run) used to
                # propagate out of run_task and crash main() with exit 1 -- discarding a
                # fully checkpointed, resumable transcript AND any partial worktree edits.
                # Observed live 2026-08-30: an Ollama 500 at iteration 7 threw away an
                # ~80%-done fix. Treat it like a graceful pause instead: fall through to
                # paused_for_review's existing save/exit path so the transcript is saved
                # and the process exits RESUMABLE (code 3), letting `--resume` (or the
                # queue's auto-resume for transient errors) pick up from here rather than
                # losing the work. NOT a verify failure -- the model's work so far stands.
                log(f"[worker] CHAT REQUEST FAILED at iteration {i}/{total_iters}: {chat_err}")
                log("[worker] pausing with a resumable transcript (exit code 3) instead of "
                    "hard-failing -- resume with --resume <transcript> to retry from here.")
                paused_for_review = f"transient chat failure: {chat_err}"
                pause_reason_code = "chat_request_failed"
                # (3) the queue row carries WHY (lane restart vs. real server error).
                pause_meta = {"lane_cause": _LANE_LAST_CAUSE[0] or str(chat_err)[:300]}
                _outer_break = True
                break
        if _outer_break:
            break
        _call_elapsed = time.monotonic() - _call_started
        usage = resp.get("usage") or {}
        _dispatch_metrics["calls"] += 1
        _dispatch_metrics["peak_prompt_tokens"] = max(_dispatch_metrics["peak_prompt_tokens"], usage.get("prompt_tokens") or 0)
        _dispatch_metrics["peak_total_tokens"] = max(_dispatch_metrics["peak_total_tokens"], usage.get("total_tokens") or 0)
        _dispatch_metrics["sum_completion_tokens"] += usage.get("completion_tokens") or 0
        usage_accumulate(_dispatch_metrics, usage, _call_elapsed)   # per-job usage sums (optional new fields; never raises)
        # Added 2026-08-28 (the owner: "can we put tok/s ... on the dashboard?") -- the
        # queue-tool API parses this line out of the job's log file to show live
        # throughput. completion_tokens is generation only (excludes prompt
        # processing), matching how tok/s is normally reported for LLM inference.
        _completion_tok = usage.get("completion_tokens") or 0
        if _call_elapsed > 0 and _completion_tok:
            log(f"[worker] iteration {i}/{total_iters} generated {_completion_tok} tokens "
                f"in {_call_elapsed:.1f}s ({_completion_tok / _call_elapsed:.1f} tok/s)")
        msg = resp.get("message", {})
        messages.append(msg)
        # Capture this turn's reasoning text for the frozen-reasoning check BEFORE
        # stripping it below -- otherwise the strip (done for every future turn's
        # prompt, not just this one's check) would blind that check to what it
        # exists to detect. Found 2026-09-20 (Opus root-cause of the bg-health-
        # s3-heartbeat stall, bece829d8005): this strip was silently missing
        # entirely, so `thinking`/`reasoning`/`reasoning_content` accumulated in
        # `messages` on EVERY dispatch regardless of preserve_reasoning, not just
        # the --preserve-reasoning (Bonsai) lane -- the same context-inflation
        # mechanism as the bonsai rep2 HTTP-400 (314KB reasoning blob -> 55 tokens
        # over n_ctx), just not yet tripped elsewhere because most jobs run with
        # more ctx headroom. Strip IN PLACE (not into a copy) because the
        # fallback-parse rewrite a few dozen lines below mutates this same `msg`
        # dict in-place relying on `messages.append(msg)` having stored a
        # reference, not a copy -- a copy here would desync that rewrite from
        # what's actually replayed on future turns.
        # REASONING_PARSER lever on: a thinking turn cut at the cap whose unclosed think block still sits in `content`
        # (probed: non-streaming + parser, reasoning empty) is re-shaped into the reasoning-only cutoff that the
        # parser's own streaming path delivers -- so it takes the think-cap recovery (thinking-off retry), is never
        # counted as prose and never replayed as assistant content.
        _unclosed = False
        if (_wl is not None and resp.get("_reasoning_parser")
                and _wl.unclosed_think_cutoff(msg, resp.get("done_reason"), usage, _turn_max_tokens,
                                              True, resp.get("_thinking_on", True))):
            _unclosed = True
            resp["_unclosed_think"] = True
            _dispatch_metrics["unclosed_think_cuts"] = _dispatch_metrics.get("unclosed_think_cuts", 0) + 1
            log(f"[worker] UNCLOSED THINK at iteration {i}/{total_iters}: reasoning parser on, finish_reason="
                f"{resp.get('done_reason')}, {len(msg.get('content') or '')} chars in content and none in "
                f"reasoning_content -- treating as a cut-off thinking turn (not prose, not replayed).")
            msg["thinking"], msg["content"] = msg.get("content") or "", ""
        _msg_thinking = msg.get("thinking") or msg.get("reasoning") or msg.get("reasoning_content") or ""
        _turn_rec = turn_log_record(i, resp, msg, _msg_thinking, usage, _call_elapsed, _turn_think)
        _dispatch_metrics.setdefault("turn_log", [])
        if len(_dispatch_metrics["turn_log"]) < TURN_LOG_MAX:
            _dispatch_metrics["turn_log"].append(_turn_rec)
        # a thinking turn CUT at the cap (parser on) is never replayed, even under preserve_reasoning: a truncated think
        # block is not a usable prior-turn thought (the recovery nudge below already carries its tail).
        _drop_cut_reasoning = _unclosed or bool(
            resp.get("_reasoning_parser")
            and think_cap_truncated(msg, usage, resp.get("done_reason"), _turn_max_tokens))
        if not preserve_reasoning or _drop_cut_reasoning:
            for _rk in ("thinking", "reasoning", "reasoning_content"):
                msg.pop(_rk, None)

        # FROZEN-REASONING CHECK (2026-09-19, BFMR read_thrash post-mortem -- see
        # reasoning_repeat_streak for the transcript evidence). Runs on the raw model
        # turn, BEFORE any tool dispatch, because the whole point is that the reasoning
        # goes stale several iterations before the tool signatures do. First repeat only
        # nudges, via the same loop_break_notes channel as loop-detect; the hard stop is
        # taken after this turn's tool calls are processed (next to `if converged: break`)
        # so a turn that also called task_complete still converges normally.
        _reasoning_streak, _reasoning_prev, _r_repeat = reasoning_repeat_streak(
            _msg_thinking, _reasoning_prev, _reasoning_streak)
        if _r_repeat:
            log(f"[worker] reasoning-freeze: this turn's thinking is >= "
                f"{REASONING_FREEZE_SIMILARITY:.2f} identical to the previous turn's "
                f"(consecutive repeat #{_reasoning_streak}).")
        if _reasoning_streak >= REASONING_FREEZE_AFTER:
            _reasoning_frozen = True
        elif _r_repeat:
            loop_break_notes.append(
                "Your reasoning this turn was essentially word-for-word identical to your "
                "previous turn. You are not making progress -- re-reading or re-running "
                "things will not change that. Either state the conclusion you already have "
                "and call task_complete, or take a genuinely different approach (a "
                "different file, a different hypothesis, a different tool). If you repeat "
                "the same reasoning once more this dispatch will be stopped."
            )

        # Context-usage counterpart to request_more_iterations, added same day at the owner's
        # request ("can we do the same for context?"): a model can't self-report running low
        # on context the way it can ask for more iterations, since it doesn't see its own
        # token accounting -- so this is harness-driven instead, checked every turn against
        # the objective usage the API already returns. Same review-gate shape: pause cleanly
        # (both loops) the moment usage crosses the threshold, resumable via `--resume
        # <transcript> --num-ctx <bigger>` once reviewed, rather than silently continuing
        # toward an actual overflow/degraded-quality response or a hard API failure.
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        if content:
            log(f"[worker] model: {content[:500]}")

        manual_call_this_turn = False
        if not tool_calls and content:
            # Universal fallback, not gated behind --manual-tools: confirmed
            # live 2026-08-21 that qwen2.5-coder:14b has a CORRECT Ollama
            # template (proper <tools> schema injection, explicit
            # instruction to respond with <tool_call>...</tool_call> and no
            # backticks) but the model itself still sometimes ignores that
            # format and wraps the same call in a ```json fence instead --
            # Ollama's native parser only recognizes the <tool_call> tag
            # form, so tool_calls came back empty even though the model's
            # intent was clearly a real tool call. extract_manual_tool_calls
            # is tag-agnostic (finds the JSON object regardless of
            # wrapper), so it catches this for ANY model as a safety net,
            # not just the templateless models --manual-tools exists for.
            # worker_robust: parse the VISIBLE text only (closed <think> blocks, an unterminated
            # <think>, and a stray </think> prefix are reasoning leakage -- a call quoted inside
            # reasoning is not a call; 5767 of the last 1500 transcripts' assistant turns carry a
            # stray </think>).
            _vis_content, _think_info = _wr.strip_think(content)
            if _bash_only:
                _bk, _bv = _wr.extract_bash_block(content)
                if _bk == "ok":
                    parsed_calls = ([{"name": "task_complete", "arguments": {"summary": "bash-only: TASK_COMPLETE"}}]
                                    if _wr.bash_mode_is_done(_bv)
                                    else [{"name": "run_bash", "arguments": {"command": _bv}}])
                else:
                    parsed_calls = []
                _cleaned_content = _vis_content
                _bash_err = None if _bk == "ok" else _wr.bash_mode_error(_bk, _bv)
            else:
                _bash_err = None
                parsed_calls, _cleaned_content = extract_manual_tool_calls(_vis_content)
            _repair = None
            if not parsed_calls and _vis_content and not _bash_only:
                # Layered repair for what the legacy parser misses: Qwen3-Coder XML with a
                # missing </function> / </parameter>, Hermes JSON cut off mid-string, inline JSON
                # with string-encoded arguments, then (profile flag, off by default) a
                # small-model extractor. A truncated mutating call is REFUSED, never run.
                _repair = _wr.repair_tool_calls(
                    _vis_content, _VALID_TOOL_NAMES, TOOLS,
                    extractor=(_wr.make_llm_extractor(
                        lambda _m: (call_ollama(host, model, _m, 0.0, num_ctx, timeout=chat_timeout,
                                                tools=False, api_style=api_style, max_tokens=1024,
                                                think=False, role=role).get("message") or {}
                                    ).get("content") or "", _VALID_TOOL_NAMES)
                               if _tool_extractor else None))
                if _repair["calls"]:
                    parsed_calls, _cleaned_content = _repair["calls"], _repair["cleaned"]
                    _dispatch_metrics["repair_layers"] = _dispatch_metrics.get("repair_layers", 0) + 1
                    log(f"[worker] tool-call repair: layer={_repair['layer']} "
                        f"truncated={_repair['truncated']} think={_think_info}")
            if parsed_calls:
                manual_call_this_turn = True
                tool_calls = [{"function": {"name": p.get("name"), "arguments": p.get("arguments", {})}}
                              for p in parsed_calls]
                log(f"[worker] fallback-parse: recovered {len(tool_calls)} tool call(s) that "
                    f"native tool_calls missed -- {[tc['function']['name'] for tc in tool_calls]}")
                if _cleaned_content != content:
                    # Break the lock-in (2026-08-29, Fable root-cause via
                    # github-projects-bf): rewrite the ALREADY-STORED assistant
                    # message in place -- messages.append(msg) above holds a
                    # reference to this same dict, not a copy, so mutating it
                    # here updates what the model sees on every future turn.
                    # Without this, a malformed XML call goes back into context
                    # verbatim and the model imitates its own prior formatting
                    # on the next response -- confirmed live: every one of
                    # these failures was malformed from iteration 1 and never
                    # recovered on its own.
                    msg["content"] = _cleaned_content
                    content = _cleaned_content
                    log("[worker] fallback-parse: rewrote the stored assistant message to drop "
                        "the salvaged XML call, so the model doesn't imitate its own malformed "
                        "formatting on the next turn.")

        if (_rev_bounds and not tool_calls and (
                think_cap_truncated(msg, usage, resp.get("done_reason"), _turn_max_tokens)
                or output_cap_cut_prose(msg, usage, resp.get("done_reason"), _turn_max_tokens))):
            # A review whose single turn fills the (small, explicit) output cap is a runaway,
            # not an answer: no retry, no recovery turn, no nudge -- a named exit.
            _dispatch_metrics["output_cap_prose_cuts"] = _dispatch_metrics.get("output_cap_prose_cuts", 0) + 1
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: bounded review -- a turn hit "
                f"the {_turn_max_tokens}-token output cap with no tool call "
                f"(~{(usage or {}).get('completion_tokens')} tokens generated) -- {_wr.REASON_REVIEW_CAP}.")
            _rev_abort = (_wr.REASON_REVIEW_CAP, f"a turn hit the {_turn_max_tokens}-token output cap")
            _dispatch_metrics["early_abort"] = _wr.REASON_REVIEW_CAP
            break

        _turn_rec["parse"] = "ok" if tool_calls else "no_tool_call"
        if tool_calls:
            _fmt_requery.on_ok()
            _runaway_streak = 0
        elif content:
            # FORMAT-ERROR REQUERY (SWE-agent forward_with_handling): content that is plainly a
            # failed tool-call attempt gets a templated error naming the exact problem, FREE
            # (the iteration is not charged) up to format_requery_max in a row, then a named
            # exit reason so the scheduler re-specs instead of grinding.
            _fe = (("bash_format", _bash_err) if _bash_only and _bash_err
                   else None if _bash_only
                   else _wr.diagnose_format_error(content, _VALID_TOOL_NAMES, TOOLS, repair=_repair))
            if _fe:
                _fe_kind, _fe_detail = _fe
                _turn_rec["parse"] = f"fail:{_fe_kind}"
                _fe_action, _fe_n = _fmt_requery.on_error(_fe_kind)
                _dispatch_metrics["format_errors"] = _dispatch_metrics.get("format_errors", 0) + 1
                if _fe_action == "stop":
                    log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_fe_n} consecutive "
                        f"unparseable tool calls (last: {_fe_kind}) after {_fmt_requery.limit} "
                        f"templated re-prompts -- {_wr.REASON_FORMAT}. Stopping instead of burning "
                        f"decode on a model that cannot emit the format.")
                    _dispatch_metrics["early_abort"] = _wr.REASON_FORMAT
                    break
                # break the lock-in AND the context bloat: the bad call (up to a whole
                # truncated file) is replaced by a stub before the model re-reads it.
                msg["content"] = (f"[a malformed tool call ({_fe_kind}, {len(content)} chars) was "
                                  f"omitted from the transcript]")
                if _fe_kind == "truncated":
                    _cap_cut_recover = True      # next turn off the near-greedy sampler
                messages.append({"role": "user", "content": _wr.format_error_message(
                    _fe_kind, _fe_detail, _fe_n, _fmt_requery.limit)})
                log(f"[worker] FORMAT ERROR ({_fe_kind}) at iteration {i}/{total_iters}: re-prompting "
                    f"{_fe_n}/{_fmt_requery.limit} WITHOUT charging an iteration.")
                i -= 1
                continue

        if not tool_calls and not content and think_cap_truncated(
                msg, usage, resp.get("done_reason"), _turn_max_tokens):
            _think_cap_streak += 1
            _dispatch_metrics["think_cap_truncations"] = _dispatch_metrics.get("think_cap_truncations", 0) + 1
            if _think_cap_streak <= THINK_CAP_RECOVERY_MAX:
                _think_cap_budget = think_cap_recovery_budget(
                    max_tokens, (usage.get("prompt_tokens") or 0)
                    + min(len(_msg_thinking), THINK_CAP_TAIL_CHARS) // 4, num_ctx)
                log(f"[worker] iteration {i}/{total_iters} spent the whole {_turn_max_tokens}-token "
                    f"output budget THINKING ({len(_msg_thinking)} chars, done_reason="
                    f"{resp.get('done_reason')}) and emitted nothing -- handing the reasoning tail "
                    f"back and retrying with num_predict={_think_cap_budget} (recovery "
                    f"{_think_cap_streak}/{THINK_CAP_RECOVERY_MAX}); not a blank answer.")
                messages.append({"role": "user",
                                 "content": think_cap_nudge(_msg_thinking, _turn_max_tokens)})
                if _rb.get("think_cap_disable_thinking", True) and _prof.get("enable_thinking") is not None:
                    _next_turn_think = False   # the thinking IS the problem: recover without it
                continue
            log(f"[worker] {_think_cap_streak} consecutive turns hit the output cap while "
                f"thinking -- recovery budget spent, falling back to the empty-answer handling.")
        elif tool_calls or content:
            _think_cap_streak = 0

        if (task_kind == "coding" or resp.get("_prose_runaway")) and output_cap_cut_prose(
                msg, usage, resp.get("done_reason"), _turn_max_tokens):
            # f21621ab528f: three turns of exactly 8192 tokens of prose, no tool call,
            # each read as a "final answer". A cut-off turn did not choose to stop.
            _dispatch_metrics["output_cap_prose_cuts"] = _dispatch_metrics.get("output_cap_prose_cuts", 0) + 1
            _cap_cut_streak += 1
            _pr_reason = resp.get("_prose_runaway")
            # DIAGNOSTIC CAPTURE: persist the raw turn BEFORE compaction destroys it.
            _co = persist_cutoff_turn(
                log_path.with_name(f"{log_path.stem}.cutoff-{i}.txt"), iteration=i,
                content=msg.get("content") or "", thinking=resp.get("_raw_thinking") or _msg_thinking,
                finish_reason=resp.get("done_reason"), usage=usage, streamed=resp.get("streamed", False),
                abort_reason=_pr_reason, max_tokens=_turn_max_tokens)
            _dispatch_metrics.setdefault("cutoff_turns", [])
            if len(_dispatch_metrics["cutoff_turns"]) < 8:
                _dispatch_metrics["cutoff_turns"].append(_co)
            log(f"[worker] cut-off turn {i}: {_co['chars']} chars, ~{_co['completion_tokens']} tokens, "
                f"dup_line_ratio={_co['dup_line_ratio']}, longest_repeated_line_block="
                f"{_co['longest_repeated_line_block']}, streamed={_co['streamed']}"
                + (f", raw turn saved to {_co['file']}" if _co.get("file") else ", raw capture FAILED"))
            for _rk in ("thinking", "reasoning", "reasoning_content"):
                msg.pop(_rk, None)   # a cut turn's reasoning is not replayed either
            _compact, _dropped = compact_cut_off_prose(msg.get("content") or "")
            if _dropped:
                # msg is the dict messages.append(msg) stored: rewriting it here
                # changes what every later turn re-reads (see the fallback-parse rewrite).
                msg["content"] = _compact
                log(f"[worker] compacted the cut-off turn in context: dropped {_dropped} "
                    f"chars of repeated/cut-off prose, kept {len(_compact)}.")
            _lad = _ladder.on_abort("cutoff")
            if _lad[0] == "exit":
                log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: sampling ladder -- {_lad[1]} "
                    f"(aborts={_ladder.aborts}, by_check={len(_ladder.by_check)}). "
                    + ("Two aborts on the same failing check: the spec-defect path comes first, not more "
                       "sampling changes." if _lad[1] == _wr.REASON_SPEC_DEFECT_REPEAT else
                       "Still looping after both steps: the scheduler re-specs."))
                _dispatch_metrics["early_abort"] = _lad[1]
                break
            if _cap_cut_streak > OUTPUT_CAP_CUT_MAX:
                log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_cap_cut_streak} "
                    f"consecutive turns were CUT OFF at the {_turn_max_tokens}-token output cap "
                    f"with no tool call, after {OUTPUT_CAP_CUT_MAX} act-now nudges -- an output-cap "
                    f"prose loop, not progress. Stopping instead of burning decode and context.")
                _dispatch_metrics["early_abort"] = "output_cap_loop"
                break
            _cap_cut_iters.append(i)
            if prose_loop_tripped(_cap_cut_iters, i):
                log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: looping in prose -- "
                    f"{PROSE_LOOP_CUTS}+ turns in the last {PROSE_LOOP_WINDOW} iterations were CUT "
                    f"OFF at the {_turn_max_tokens}-token output cap with no tool call "
                    f"(cut iterations {_cap_cut_iters[-PROSE_LOOP_CUTS:]}). Stopping instead of "
                    f"burning decode and context.")
                _dispatch_metrics["early_abort"] = "prose_loop"
                break
            if _pr_reason:
                log(f"[worker] iteration {i}/{total_iters} was ABORTED by the prose-runaway guard "
                    f"({_pr_reason}) with no tool call -- telling the model to act and continuing.")
                _pan = ""
                if _wl is not None and _wl.flag("PROSE_ACT_NUDGE") and task_kind == "coding":   # default OFF; A/B first
                    try:
                        _pan = _wl.act_nudge_suffix(
                            _worktree_start_snapshot is not None
                            and _git_worktree_snapshot(cwd) == _worktree_start_snapshot)
                    except Exception:
                        _pan = ""
                messages.append({"role": "user", "content": prose_runaway_nudge(
                    _PROSE_BUDGET_TOKENS, _pr_reason, task_kind) + _pan})
            else:
                log(f"[worker] iteration {i}/{total_iters} was CUT OFF at the {_turn_max_tokens}-token "
                    f"output cap mid-prose with no tool call -- not a final answer; telling the model "
                    f"to act and continuing.")
                messages.append({"role": "user", "content": output_cap_cut_nudge(_turn_max_tokens)})
            _cap_cut_recover = True
            # Darkbloom returns Qwen thinking INSIDE content (no reasoning field), so a thinking-mode
            # no-tool-call runaway is the chain of thought itself. Retry the next turn non-thinking
            # (probe 2026-10-09: enable_thinking=false answers cleanly; default thinks in content).
            if _prof.get("enable_thinking") is not None:
                _next_turn_think = False
            if _wl is not None:     # PROSE_STICKY_NOTHINK: the FIRST abort makes the recovery profile permanent (flag off = no-op)
                _wl.l4_note_abort(role, _prof.get("enable_thinking"), log=log)
            continue

        if tool_calls:
            _cap_cut_streak = 0
            _ladder.on_ok()
        try:
            _loop_det.observe_turn(bool(tool_calls))
        except Exception as _ld_err:
            log(f"[worker] loop-detector observe_turn error (ignored): {_ld_err!r}")
        if not tool_calls:
            # Confirmed live 2026-08-21 (devstral:24b): a model can narrate
            # code in a fenced block instead of calling write_file, even
            # with an explicit system-prompt instruction not to. Give
            # exactly ONE corrective nudge if the response looks like a
            # narrated/summarized non-answer instead of real tool use --
            # bounded, not a loop, structurally different from opencode's
            # confirmed infinite self-nudge bug (that one re-injected a
            # generic "continue" with no new information forever; this
            # injects a specific correction once).
            #
            # Originally only fired on a code fence with no tool call, but
            # confirmed live 2026-08-21 (qwen3-coder-next) that a model can
            # also just write a plain-English summary of the files it read
            # (no fence at all) and stop having done zero edits -- same
            # underlying failure (treating description as the deliverable),
            # so the trigger is now "no mutation has happened yet at all" (see
            # any_mutation_called above for why read-only tool calls don't count).
            # Fable gap-fix 2026-08-29 (the owner: rv6-control-checklist-r1 "said done but is on
            # iteration 40/41"): the mutation-count nudge below fires when any_mutation_called
            # is False -- but that flag only counts write_file/edit_file, so a task whose
            # deliverable is written via a run_bash heredoc (the review-bench pattern: the model
            # `cat > REVIEW.md <<EOF`'d a complete 17KB review, zero write_file calls) reads as
            # "no progress" and gets nudged away from a CORRECT completion, three times, burning
            # the budget. When a coding task has a --verify command, that command is a strictly
            # better "is it actually done" signal than the mutation counter -- so defer to the
            # verify-based silence gate below (which runs verify and either converges on a pass
            # or gives a SPECIFIC verify-failure nudge) instead of firing this generic one. The
            # generic nudge stays the only safeguard when there's no verify to consult.
            defer_to_verify_gate = task_kind == "coding" and bool(verify)
            if (nudge_count < MAX_NUDGES and tool_called_since_last_nudge
                    and not any_mutation_called and not defer_to_verify_gate):
                nudge_count += 1
                tool_called_since_last_nudge = False
                log(f"[worker] no tool call yet and none made this response -- "
                    f"sending corrective nudge {nudge_count}/{MAX_NUDGES} instead of "
                    f"accepting it as final.")
                # Confirmed live 2026-08-28 (qwen3:8b, twice, identically): this
                # nudge originally hardcoded "e.g. write_file" as the example
                # action, which is a CODING-task assumption baked into a
                # mechanism meant to apply to every dispatch. On a research
                # task, the model responded to the nudge by literally calling
                # write_file to save its already-written (and unverified/
                # fabricated) answer to a file -- technically satisfying the
                # nudge's narrow check ("was a tool called") while doing
                # nothing to fix the actual problem (it still hadn't fetched
                # a source to back its claims). The nudge was steering toward
                # the wrong action, not just failing to help. Reworded to be
                # task-type-agnostic: point back at what the task itself
                # asked for, and name a read tool (web_fetch) as an equally
                # valid example alongside a write tool, so a research
                # dispatch isn't nudged toward writing a file it was never
                # asked to write.
                if "</tool_call>" in content or "<function=" in content:
                    # Fable's question #3 (2026-08-29, via github-projects-bf): when the
                    # content shows signs of a malformed tool-call attempt that salvage
                    # couldn't fully recover (a dangling </tool_call>, or a <function=...>
                    # whose name didn't validate), the generic nudge above is unactionable
                    # -- the model believes it DID make a call, so "call a tool" doesn't
                    # tell it anything new. Quote the exact expected format, INCLUDING the
                    # opening tag it's apparently dropping, instead.
                    messages.append({
                        "role": "user",
                        "content": "Your last response looks like an attempted tool call that "
                                   "wasn't recognized -- check that you opened it correctly. "
                                   "The exact format is:\n<tool_call>\n<function=NAME>\n"
                                   "<parameter=KEY>\nVALUE\n</parameter>\n</function>\n"
                                   "</tool_call>\nMake sure <tool_call> opens the block -- a "
                                   "response with only the closing </tool_call> and no opener "
                                   "is not recognized as a real call.",
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": "You have not made real progress on the task yet -- "
                                   "describing, printing, or summarizing does not count as "
                                   "doing the work. Call whichever tool actually advances what "
                                   "the task asked for -- that might be write_file/edit_file if "
                                   "the task wants a code change, or web_fetch to verify a claim "
                                   "before stating it, or another tool entirely, depending on "
                                   "what THIS task needs. Do not call a tool just to satisfy this "
                                   "message if it doesn't genuinely move the task forward.",
                    })
                continue
            # Confirmed live 2026-08-28 (llama3.1:8b, EV-charging-network research, several
            # consecutive attempts): a model can find exactly the right pages via web_search
            # repeatedly and just never call web_fetch on them, converging on snippet-only
            # claims every time even with iteration budget to spare. A ONE-TIME nudge here
            # proved genuinely insufficient in practice -- confirmed live: a Tesla research
            # dispatch got nudged once at 1/3 required fetches, made exactly one more real
            # fetch attempt, then gave up and was accepted at 1/3 anyway. The owner's standing
            # instruction: when Unraid research comes back incomplete, the mechanism should be
            # strengthened and retried, not just reported thin. Bounded to MAX_MIN_FETCH_NUDGES
            # (matching the file's other bounded-nudge conventions) instead of exactly one, so
            # the requirement actually has teeth without being literally unbounded.
            if (task_kind == "research" and min_web_fetches > 0
                    and web_fetch_success_count < min_web_fetches
                    and min_fetches_nudge_count < MAX_MIN_FETCH_NUDGES):
                min_fetches_nudge_count += 1
                log(f"[worker] research task converging with only {web_fetch_success_count}/"
                    f"{min_web_fetches} required successful web_fetch calls -- sending "
                    f"corrective nudge {min_fetches_nudge_count}/{MAX_MIN_FETCH_NUDGES} before "
                    f"accepting the answer.")
                messages.append({
                    "role": "user",
                    "content": f"You have successfully fetched {web_fetch_success_count} real "
                               f"page(s) so far, but this task requires at least "
                               f"{min_web_fetches}. Search results alone are not enough -- "
                               f"call web_fetch on real URLs (ones you haven't already "
                               f"successfully fetched) until you reach that minimum, then give "
                               f"your final answer. If a specific site keeps failing, try a "
                               f"genuinely different site, not a reworded search for the same one.",
                })
                continue
            if should_send_zero_fetch_nudge(task_kind, web_fetch_succeeded, local_read_count,
                                            facts_provided, fabrication_nudge_sent):
                fabrication_nudge_sent = True
                log("[worker] research task converging with zero successful web_fetch calls -- "
                    "sending one corrective nudge before accepting the answer.")
                messages.append({
                    "role": "user",
                    "content": "Every web_fetch call in this session has failed -- you have not "
                               "actually read a real source. Any specific number, date, name, or "
                               "other concrete detail in your answer so far is UNVERIFIED, not a "
                               "confirmed fact. Rewrite your final answer: for each item you cannot "
                               "trace back to real fetched content, say plainly you could not "
                               "confirm it (name what you would check next) instead of presenting "
                               "it as a settled fact with a confidence label. Do not fabricate a "
                               "number just to fill in the answer.",
                })
                continue
            # Confirmed live 2026-08-28 (qwen3:8b-tuned llama3.1:8b via a review fork,
            # EV-charging-network discovery): the branch above only guards against ZERO
            # successful fetches -- a model that makes real, successful fetches can still state
            # specific numbers that don't appear anywhere in what it actually fetched (two real
            # Wikipedia fetches; the specific station-count figures it then stated were
            # confirmed absent from both). Genuine collect-mode research had no equivalent of
            # facts-provided mode's grounding check. Reuses the same find_ungrounded_numeric_
            # claims helper, but against only the fetched content this session (not task text,
            # which for real research is the question, not a source of facts).
            if (task_kind == "research" and web_fetch_succeeded and not facts_provided
                    and not fabrication_nudge_sent):
                ungrounded_collect = find_ungrounded_numeric_claims(
                    content, "\n".join(real_fetched_texts))
                if ungrounded_collect:
                    fabrication_nudge_sent = True
                    log(f"[worker] research answer (real fetches happened) contains "
                        f"{len(ungrounded_collect)} numeric/time claim(s) not found in "
                        f"anything actually fetched this session -- sending one corrective "
                        f"nudge before accepting the answer: {ungrounded_collect}")
                    messages.append({
                        "role": "user",
                        "content": "Your answer states the following specific figures that do "
                                   "NOT appear anywhere in the content you actually fetched: "
                                   + "; ".join(ungrounded_collect) + ". These look fabricated -- "
                                   "having made a real fetch elsewhere doesn't make an unrelated "
                                   "invented number acceptable. Rewrite your final answer: for "
                                   "each one, either point to exactly which fetched source it "
                                   "comes from, or replace it with an explicit 'could not "
                                   "confirm this figure from what I fetched' statement. Do not "
                                   "invent a number to fill the gap.",
                    })
                    continue
            # facts-provided mode has no web_fetch signal to check (zero fetches is
            # expected by design), so it needs its own grounding check instead of the
            # branch above -- see find_ungrounded_numeric_claims for why this exists.
            if (task_kind == "research" and facts_provided and not fabrication_nudge_sent):
                # Confirmed live 2026-08-28 (llama3.1:8b, NV-Energy retry): the task text
                # explicitly permits "at most one targeted fetch" if a supplied fact proves
                # insufficient -- a model that does exactly that and gets real new content
                # was still flagged, because the grounding source was only the ORIGINAL
                # facts, not anything legitimately fetched this session. Include every tool
                # result's content too, so a real verified fetch counts as grounding.
                ungrounded = find_ungrounded_numeric_claims(
                    content, task + "\n" + "\n".join(real_fetched_texts))
                if ungrounded:
                    fabrication_nudge_sent = True
                    log(f"[worker] facts-provided research answer contains "
                        f"{len(ungrounded)} numeric/time claim(s) not found in the "
                        f"supplied facts -- sending one corrective nudge before "
                        f"accepting the answer: {ungrounded}")
                    messages.append({
                        "role": "user",
                        "content": "Your answer states the following specific figures that do "
                                   "NOT appear anywhere in the facts you were given: "
                                   + "; ".join(ungrounded) + ". These look fabricated. Rewrite "
                                   "your final answer: for each one, either point to exactly "
                                   "where in the provided facts it comes from, or replace it "
                                   "with an explicit 'could not confirm this figure from the "
                                   "provided facts' statement. Do not invent a number to fill "
                                   "the gap.",
                    })
                    continue
            # Confirmed live 2026-08-28 (qwen3.5:9b, NV-Energy round 2): a model can stop with
            # BOTH zero tool calls AND empty content -- the checks above all key off `content`
            # having something in it (nudge text, fabrication claims), so a genuinely blank
            # response sailed through everything and got accepted as "the final answer" with
            # nothing in it at all. Catch this explicitly before accepting anything.
            if not content.strip() and not empty_answer_nudge_sent:
                empty_answer_nudge_sent = True
                log("[worker] response has no tool calls AND no content -- sending one "
                    "corrective nudge instead of accepting a blank final answer.")
                messages.append({
                    "role": "user",
                    "content": "Your last response was empty -- no tool call, no text. Either "
                               "call a tool to keep working, or write out your actual final "
                               "answer now. An empty response is not acceptable as the final "
                               "answer.",
                })
                continue
            # Confirmed live 2026-08-28 (llama3.1:8b, EV-charging-network discovery dispatch):
            # the fabrication nudge above is a ONE-TIME correction, and a model can respond to
            # it by making more unsuccessful/absent web_fetch attempts and then simply CLAIM
            # verification it never had -- this run's final answer stated a specific figure was
            # "verified through web_fetch" when zero web_fetch calls succeeded anywhere in the
            # entire session (confirmed directly against the raw transcript: every tool result
            # was either a web_search snippet or a web_fetch ERROR). Prompting alone clearly
            # isn't reliable here, so this is an unconditional, harness-level warning appended
            # after the fact -- it doesn't depend on the model being honest about its own
            # process, only on the objective, harness-tracked fact of whether a fetch ever
            # actually succeeded.
            # facts_provided excluded: confirmed live 2026-08-28 (EV-charging pass-2 dispatch)
            # that this fired on a genuinely honest, correctly-sourced answer synthesized from
            # facts real-fetched in a PRIOR pass -- zero fetches THIS session is expected and
            # correct there by design, not a red flag, and facts-provided mode already has its
            # own real grounding check (find_ungrounded_numeric_claims above) for this exact
            # concern. This banner is for genuine collect-mode research with no verification at
            # all, not for pass-2 synthesis correctly skipping a fetch it was told not to do.
            # local_read_count gate added 2026-09-06 (false-signal fix, job 8d690b764cec):
            # the old condition was `not web_fetch_succeeded` ALONE, which stamped this
            # unverified warning on EVERY research dispatch that made zero web_fetch calls
            # -- including the large, legitimate class whose real sources are LOCAL repo
            # files (trace a call path, find why X is gated on Y, locate a perf hotspot).
            # Those tasks correctly use read_file/grep and correctly make zero web_fetch
            # calls, and stamping "claims can't be verified" on them is a FALSE signal: the
            # answer WAS verified, just against the repo instead of the web. The warning is
            # for the case it was actually built for -- a research answer that shows NO
            # verification of ANY kind. So require BOTH zero web_fetch AND zero local reads:
            # a task that read/grepped the tree has shown genuine local verification and is
            # exempt; a task that fetched nothing AND read nothing local (the real "confident
            # answer, no sources touched" failure) is still flagged, web-research included.
            if should_stamp_unverified(task_kind, web_fetch_succeeded,
                                       local_read_count, facts_provided):
                warning = ("\n\n---\nHARNESS WARNING: no web_fetch call succeeded anywhere in "
                           "this session. Any claim above of having 'verified' or 'confirmed' a "
                           "detail is NOT reliable, regardless of what the text above says -- "
                           "treat every specific fact in this answer as unconfirmed until it is "
                           "checked against a source that was actually, successfully fetched.")
                msg["content"] = (msg.get("content") or "") + warning
                log("[worker] research task converged with zero successful web_fetch calls AND "
                    "zero local reads all session -- appending unconditional harness warning "
                    "(no verification of any kind; the model's claims can't be trusted).")
            elif (task_kind == "research" and not web_fetch_succeeded
                    and local_read_count > 0 and not facts_provided):
                log(f"[worker] research task converged with zero web_fetch but "
                    f"{local_read_count} local read(s) -- NOT stamping the unverified warning "
                    f"(local-source verification is genuine; web-provenance expectation "
                    f"does not apply to a local-repo investigation).")
            # Same task_complete verify-gate as above, applied to the silent path -- a model
            # that just stops (no tool call at all) gets the same chance to see WHY it isn't
            # done instead of the harness accepting it blind and finding out only at the
            # authoritative end-of-run verify. Bounded (MAX_COMPLETION_VERIFY_NUDGES) so a
            # task whose verify can never be satisfied still converges eventually, same as
            # the model's own word being accepted after MAX_COMPLETION_CLAIMS above.
            if task_kind == "coding" and verify:
                # Same baseline-delta classification (and same soundness gates) as the
                # task_complete gate above -- see there for the conditions' full rationale.
                v_ok, new_failures, preexisting, current_recognized, v_out = (
                    _verify_delta_feedback(verify, cwd, _baseline_verify_sig))
                if _bp is not None:
                    try:
                        _bp.observe_verify(i, v_out, v_ok)
                    except Exception:
                        pass
                _ladder.note_check(_verify_failure_signature(v_out) if not v_ok else None)
                _did_work = (_files_modified_count > 0 or _run_bash_success_count > 0)
                _no_regression = (bool(_baseline_verify_sig) and current_recognized
                                   and not new_failures)
                _diff_new = attributable_new_failures(new_failures, _baseline_verify_sig)
                _decision = silent_stop_decision(
                    v_ok, _diff_new, completion_verify_nudges, MAX_COMPLETION_VERIFY_NUDGES,
                    _baseline_is_the_task, _no_regression, _did_work)
                if _decision == "nudge":
                    # Something to fix. Failures THIS diff introduced are listed alone,
                    # as caused by the model's edits (new_failure_feedback) and never
                    # end the run (silent_stop_decision) -- f21621ab528f. Otherwise the
                    # previous behaviour: the raw output, bounded by the nudge cap.
                    #
                    # Batch #7/#8: _baseline_is_the_task forces the nudge even on a clean
                    # no-regression reading -- a verify failing by design before the model
                    # started must PASS, "0 new failures" is what an untouched bug produces.
                    completion_verify_nudges += 1
                    # the verify output is NEW information handed to the model, bounded by its own cap:
                    # not a monologue streak (worker_robust.LoopDetector.monologue_verdict)
                    _loop_det.no_tool_streak = 0
                    if _diff_new:
                        _fb = new_failure_feedback(_diff_new, v_out, _baseline_is_the_task)
                    elif _baseline_is_the_task:
                        _fb = ("the verify still fails:\n" + v_out[-3000:] +
                               "\n(This verify was ALREADY FAILING before you started -- by "
                               "design. Those failures ARE the bug you were asked to fix; you "
                               "are not done until this command PASSES.)")
                    else:
                        _fb = "the verify still fails:\n" + v_out[-3000:]
                    log(f"[worker] silent final answer given but verify still fails "
                        f"({len(_diff_new)} introduced by this diff) -- nudge "
                        f"{completion_verify_nudges} (cap {MAX_COMPLETION_VERIFY_NUDGES}, not "
                        f"applied while this diff's failures remain), feeding back instead of "
                        f"accepting.")
                    messages.append({
                        "role": "user",
                        "content": f"You stopped without calling a tool, but {_fb}\nFix them, "
                                   f"then call task_complete once the verify passes.",
                    })
                    continue
                elif not v_ok:
                    log("[worker] silent final answer given, verify fails but ONLY on pre-existing "
                        "baseline failures (0 new from this diff) or the nudge cap is spent -- "
                        "accepting the stop (BASELINE-BROKEN / cap).")
            converged = True
            log("[worker] no tool calls in response -- treating as final answer, stopping.")
            break

        tool_called_since_last_nudge = True
        # PER-TURN FAN-OUT CAP (command-r fired 73 identical blind web_search calls in
        # one turn). Truncate a runaway turn so it can't burn the whole budget / hammer
        # a backend; the model re-issues anything it still needs next turn.
        if isinstance(tool_calls, list) and len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
            log(f"[worker] per-turn tool-call fan-out cap: {len(tool_calls)} calls in "
                f"one turn, truncating to {MAX_TOOL_CALLS_PER_TURN}")
            _fan_v = _fan_guard.on_turn(len(tool_calls))
            tool_calls = tool_calls[:MAX_TOOL_CALLS_PER_TURN]
            # Keep the stored assistant message consistent with what actually ran (tool results exist
            # only for the executed calls), then name the offence and hand the model a corrective.
            if isinstance(msg.get("tool_calls"), list) and len(msg["tool_calls"]) > MAX_TOOL_CALLS_PER_TURN:
                msg["tool_calls"] = msg["tool_calls"][:MAX_TOOL_CALLS_PER_TURN]
            _dispatch_metrics["tool_fanout_offences"] = _fan_guard.offences
            if _fan_v:
                if _fan_v[0] == "warn":
                    loop_break_notes.append(_fan_v[2])
                elif _fan_abort is None:
                    _fan_abort = (_fan_v[1], _fan_v[2])
                # Sampling ladder (A/B arm, default OFF): a runaway turn is an abort step. Inert when
                # the arm is off (on_abort returns ("off", None)).
                _lad = _ladder.on_abort("tool_fanout")
                if _lad[0] == "exit" and _fan_abort is None:
                    _fan_abort = (_lad[1], "sampling ladder: " + _fan_v[2])
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            if task_kind == "research" or name in ("write_file", "edit_file"):
                any_mutation_called = True
            raw_args = fn.get("arguments", {})
            try:
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
            except (json.JSONDecodeError, TypeError) as _arg_err:
                # A tool call whose JSON arguments are truncated/malformed must NOT crash the
                # whole worker. json.loads used to raise here uncaught, killing the dispatch --
                # confirmed live 2026-09-13 (bo-B-q8 arm: a write_file for refimpl.py was cut
                # off mid-string at the 8192-token per-turn cap, leaving unterminated JSON, and
                # the worker died with exit 1). Feed it back as a recoverable bad-tool-call so
                # the model re-emits it (ideally smaller / chunked) instead of the run dying.
                _tcid = tc.get("id")
                _preview = (raw_args if isinstance(raw_args, str) else str(raw_args))[:200]
                _result = (f"ERROR: your {name} tool-call arguments were not valid JSON "
                           f"({_arg_err}). This almost always means the call was truncated by "
                           f"the per-turn token limit while writing a long value. Re-issue the "
                           f"call with a smaller payload: for a large file, write it in several "
                           f"smaller write_file/edit_file calls rather than one. First ~200 "
                           f"chars received: {_preview!r}")
                log(f"[worker] tool-call arg parse failed for {name}: {_arg_err} -- recovering "
                    f"(feeding a bad-tool-call error back to the model, not crashing)")
                if api_style == "openai":
                    _tm = {"role": "tool", "content": str(_result)}
                    if _tcid:
                        _tm["tool_call_id"] = _tcid
                    messages.append(_tm)
                else:
                    messages.append({"role": "tool",
                                     "content": f"[tool result for {name}]: {_result}"})
                continue
            # COHERE/command-r arg-envelope unwrap: {"tool_name":..,"parameters":{..}}
            # -> the flat args the rest of the loop (args.get("query"), etc.) expects.
            # No-op for every model that already emits flat args. (Fix 3.)
            args = _unwrap_tool_args(name, args, log)
            if live is not None:
                live.tool_call(name, args)
            tool_call_id = tc.get("id")

            if name == "request_more_iterations":
                # Special-cased here rather than in tool_impls/build_tool_impls: it needs to
                # stop the whole dispatch, not just return a tool result. The owner's call
                # 2026-08-28: this is a REVIEW GATE, not an auto-grant or a wait-with-timeout --
                # no in-process polling at all. The request pauses the dispatch cleanly (same
                # incremental-save mechanism that already makes every DID-NOT-CONVERGE stop
                # resumable), and a human/Claude reviews it on their own time, then resumes with
                # `--resume <transcript> --max-iters N` -- reusing the exact resume flow already
                # proven live tonight (the queue-tool-build resume), rather than inventing a new
                # side-channel. No response is recorded for this call since the dispatch stops
                # before there's anywhere to deliver one -- resuming re-adds the same tool result
                # naturally via the next model turn once it's given more budget.
                requested = args.get("additional")
                reason = args.get("reason", "")
                requested = requested if isinstance(requested, int) and requested > 0 else 0
                paused_for_review = (
                    f"model requested +{requested} more iterations (currently at {i}/"
                    f"{total_iters}), reason: {reason!r}"
                )
                pause_reason_code = "request_more_iterations"
                pause_meta = {"requested_additional": requested, "reason": reason}
                log(f"[worker] PAUSED FOR REVIEW: {paused_for_review}. To grant, resume with: "
                    f"--resume {log_path} --max-iters <N> (in addition to the same other flags "
                    f"this dispatch used). To deny, just don't -- the transcript stays as-is.")
                break

            sig = f"{name}:{json.dumps(args, sort_keys=True)}"
            if name == "run_bash" and re.match(r"^\s*echo\b", str(args.get("command", ""))):
                # Fable finding 2026-08-29: a model trying to signal "I'm done" through the
                # only channel it reliably uses (a tool call, not silence) tends to reach for
                # a ritual `run_bash echo "..."` -- confirmed live, 18 consecutive iterations,
                # varying the echoed text just enough (checkmarks, phrasing) that the exact-
                # args signature below never repeated identically and loop-detect never fired.
                # Collapse every echo-only run_bash call to one signature regardless of what it
                # echoes, so this pattern actually trips the existing 3x loop-detect guidance
                # instead of evading it for the entire run.
                sig = "run_bash:<echo>"
            # A model re-reading the SAME page of the same file is the specific
            # loop this paging change can create: page 1 answers the question
            # "what's in this file" badly, so it asks again identically instead
            # of advancing the offset. The existing loop-detect only counts
            # FAILED calls, and a read that returns page 1 is a success every
            # time -- so it would never fire. Count successful same-page reads
            # separately and hand back the arithmetic rather than a scolding.
            if _rev_bounds and _rev_abort is None:
                _rdetail = _rep_guard.observe(_wr.repeat_call_key(
                    name, args, lambda _p: resolve_path(cwd, _p)))
                if _rdetail:
                    _rev_abort = (_wr.REASON_REPEAT_CALL, _rdetail)
            if name == "read_file" and isinstance(args, dict):
                _rk = (str(args.get("path")), str(args.get("offset") or 1))
                _repeat_reads[_rk] = _repeat_reads.get(_rk, 0) + 1
            # Is this the job's OWN verify command? (Re-running it after every edit is
            # exactly what a converge-on-verify task instructs -- never treat that as a
            # loop, and never serve it from the anti-thrash cache.) Computed here so the
            # anti-thrash intercept just below and the loop-detect further down share it.
            _is_own_verify = False
            if verify and name == "run_bash" and isinstance(args, dict):
                _c = " ".join(str(args.get("command", "")).split())
                _v = " ".join(str(verify).split())
                _is_own_verify = _c == _v or _c.startswith(_v)
            # A read-only inspection whose repeat is safe to serve from cache: reads and
            # listings always, a run_bash only when it is a local read (grep/cat/find/...),
            # never a mutation and never the verify.
            _thrash_cacheable = (not _is_own_verify) and (
                name in ("read_file", "list_files")
                or (name == "run_bash" and isinstance(args, dict)
                    and _command_is_local_read(str(args.get("command", "")))))
            _thrash_cached, _thrash_nudge = _anti_thrash_intercept(
                sig, _thrash_cacheable, _tool_result_cache, _thrash_repeats)
            impl = tool_impls.get(name)
            _lg_block, _lg_prefix = (_lguards.web_search(cwd, args) if name == "web_search" else (None, ""))
            if name == "task_complete":
                _lguards.on_task_complete()
            if _bp is not None and isinstance(args, dict):
                try:
                    _bp.before_tool(name, args, cwd)
                except Exception:
                    pass
            # SILENT ARG-DROPPING IS WHAT MADE THE read_file BUG INVISIBLE. The
            # model asked for offset/length, the schema declared neither, and
            # the harness executed the call anyway as though the arguments had
            # never been sent -- so the model saw a successful read and had no
            # way to learn its request was ignored. It repeated the call. Any
            # argument the schema does not declare now comes back as a visible
            # note appended to the result, never as a refusal: the call still
            # runs, because rejecting it would break working dispatches over a
            # stray key, but the model is told what was ignored.
            _unknown = sorted(set(args) - _tool_arg_names(name)) if isinstance(args, dict) else []
            if repeated_failures.get(sig, 0) >= 3:
                # HARD BLOCK. Warning alone was not enough: confirmed live
                # 2026-08-22 (qwen2.5-coder:7b, v4) that after the advisory
                # loop-break message the model retried the same dead path
                # anyway, reaching 11 identical failures and burning the run.
                # Refusing to execute is the only thing that reliably forces
                # a different action.
                result = (f"REFUSED: you have already called {name} with these exact arguments "
                          f"3+ times and it failed every time. This path does not exist. Stop "
                          f"retrying it. Call list_files on '.' to see the real structure, and "
                          f"use only paths that appeared in a list_files result.")
            elif name == "run_bash" and _command_risks_self_collision(args.get("command", "")):
                # Harness-level block, not a prompt-level ask -- see
                # _command_risks_self_collision's docstring/comment above.
                result = (f"REFUSED: this command appears to call an LLM inference endpoint "
                          f"(Ollama's /api/generate|chat|embed|pull|create, an OpenAI-style "
                          f"/v1/chat/completions|completions|embeddings path, or `ollama run|pull|"
                          f"create`) directly. You are already running as a resident model on this "
                          f"host -- a second inference call risks loading another model into the "
                          f"same VRAM/memory pool and crashing this dispatch (confirmed: this exact "
                          f"pattern OOM'd a prior run). Do not test, warm up, or call any inference "
                          f"endpoint yourself. If you need to verify an API's request/response "
                          f"shape, reason about it from documentation/what you already know instead "
                          f"of making a live call.")
            elif name == "task_complete":
                # Fable design 2026-08-29 (the owner: "how do we fix so we get it to converge?"),
                # built against real transcripts where coding-tuned models structurally
                # avoided the old silence-only convergence signal (some never emitted a
                # single content-only turn across 13-30 iterations) while also, separately,
                # declaring victory on objectively incomplete work that a weak --verify
                # blessed. Gate the claim on the task's own verify command so a false
                # completion claim comes back as concrete feedback instead of being either
                # accepted blind or silently discarded at DID-NOT-CONVERGE. Bounded by
                # MAX_COMPLETION_CLAIMS so a task whose verify can never be satisfied still
                # stops eventually instead of looping the claim forever.
                completion_claims += 1
                if verify and completion_claims <= MAX_COMPLETION_CLAIMS:
                    # Baseline-delta feedback (added 2026-08-30): classify the verify result
                    # against the start-of-dispatch baseline so the model is told only about
                    # failures ITS diff introduced, not pre-existing noise it can't and
                    # shouldn't fix (see _verify_failure_signature's docstring for the incident).
                    v_ok, new_failures, preexisting, current_recognized, v_out = (
                        _verify_delta_feedback(verify, cwd, _baseline_verify_sig))
                    if _bp is not None:
                        try:
                            _bp.observe_verify(i, v_out, v_ok)
                        except Exception:
                            pass
                    _ladder.note_check(_verify_failure_signature(v_out) if not v_ok else None)
                    # Evidence the model actually did work this session -- required before a
                    # BASELINE-BROKEN accept so a zero-edit claim on a broken baseline can't
                    # exit clean (Fable review 2026-08-30, condition 2: mirrors the end-of-run
                    # vacuous-pass guard, which the BASELINE-BROKEN path would otherwise skip).
                    _did_work = (_files_modified_count > 0 or _run_bash_success_count > 0)
                    # A no-regression accept is only SOUND when: the baseline genuinely had
                    # pre-existing failures (non-empty set, not a passing baseline's empty set
                    # and not a None/legacy baseline), we RECOGNIZED the current failing output
                    # (else an unrecognized new failure yields an empty delta and false-accepts
                    # -- Fable condition 1), and every recognized current failure is pre-existing.
                    _no_regression = (bool(_baseline_verify_sig) and current_recognized
                                       and not new_failures)
                    if v_ok and _baseline_is_the_task and not _did_work:
                        # Batch #7: the TRUE anomaly, and the only pause left on this path.
                        # This verify was PROVEN failing before the model touched anything,
                        # and it now passes over an UNCHANGED tree (no file writes, no
                        # successful run_bash). Nothing this run did can explain the flip, so
                        # the verify is nondeterministic or depends on state outside the
                        # worktree -- under either reading its pass is not evidence of a fix,
                        # and accepting would score an untouched bug as solved. Unlike a
                        # still-failing verify (which the model can act on, see below), there
                        # is nothing to feed back here: the defect is in the verify itself.
                        log(f"[worker] task_complete claim {completion_claims}: verify now PASSES "
                            f"but it was PROVEN FAILING at baseline and this run changed nothing "
                            f"(0 file edits, 0 successful run_bash) -- the verify flipped on its "
                            f"own. Pausing rather than scoring an untouched tree as a pass.")
                        pause_reason_code = "verify_flipped_without_work"
                        paused_for_review = (
                            "verify failed at baseline and now passes over an unchanged tree: it "
                            "is nondeterministic or depends on state outside the worktree, so its "
                            "pass proves nothing. Check the verify command by hand.")
                        result = ("NOT ACCEPTED: the verify passes now, but it was failing before "
                                  "your work began and you have not changed any file. This run is "
                                  "paused for a human to look at.")
                        converged = False
                    elif v_ok:
                        result = "ACCEPTED: verify passed."
                        converged = True
                        final_summary = args.get("summary", "")
                    # Reads the run_task PARAMETER, not `args`. In this scope `args`
                    # is the TOOL-CALL dict (see args.get("summary") directly above),
                    # so getattr(args, "verify_failed_at_baseline", False) returned the
                    # default False unconditionally and this branch was unreachable --
                    # the guard was deployed and inert, which is why three jobs were
                    # false-accepted on 2026-09-01 with the fix supposedly in place.
                    elif _baseline_is_the_task:
                        # Batch #7, replacing an immediate pause (2026-09-02). This verify was
                        # already failing before the model started -- for a bug-fix verify that
                        # is BY DESIGN, so "no NEW failures" is exactly what a NON-fix produces
                        # and must never be accepted (measured: three consecutive resell #310
                        # dispatches were accepted this way while the bug survived untouched).
                        #
                        # But it is not a reason to STOP. The old code paused here on the first
                        # claim, which is what parked scored arms at 21/30 with iterations
                        # unspent and a human on the critical path -- a run that still had every
                        # resource it needed to finish. The honest move is neither accept nor
                        # pause: tell the model the truth (the bar is a PASSING verify, not an
                        # unchanged one), hand it the FULL output -- NO baseline subtraction,
                        # because here the pre-existing failures ARE the task (batch #8) -- and
                        # let it keep working. The pause moves to the claim cap below, where the
                        # iterations really are spent and a human is genuinely needed.
                        log(f"[worker] task_complete claim {completion_claims}/"
                            f"{MAX_COMPLETION_CLAIMS}: verify still FAILS and its baseline was "
                            f"already failing at stage/enqueue -- NOT accepting (a still-failing "
                            f"verify cannot show the fix landed) and NOT pausing: feeding the "
                            f"full output back and continuing, claims remain.")
                        _dn = attributable_new_failures(new_failures, _baseline_verify_sig)
                        if _dn:
                            result = ("NOT ACCEPTED: " + new_failure_feedback(_dn, v_out, True) +
                                      "\nKeep working. Do not run the verify command yourself "
                                      "-- the harness already did.")
                        else:
                          result = (f"NOT ACCEPTED: the task's verification command still fails.\n"
                                  f"--- verify output ---\n{v_out[-3000:]}\n--- end ---\n"
                                  f"IMPORTANT: this verify was ALREADY FAILING before you started "
                                  f"-- that is by design for this task. So 'I introduced no new "
                                  f"failures' is NOT success here: those pre-existing failures ARE "
                                  f"the bug you were asked to fix. You are not done until this "
                                  f"command PASSES (exits 0). Keep working. Do not run the verify "
                                  f"command yourself -- the harness already did.")
                        converged = False
                    elif _no_regression and _did_work:
                        # Verify still fails, but every recognized current failure was ALSO present
                        # at dispatch start -- the model's diff introduced no new failures. Do NOT
                        # loop the claim on pre-existing noise (the convergence failure this fixes):
                        # accept, with a loud BASELINE-BROKEN marker. The end-of-run authoritative
                        # verify applies the identical rule (+ the same evidence-of-work guard).
                        log(f"[worker] task_complete claim {completion_claims}: verify still fails "
                            f"BUT 0 new recognized failures vs the dispatch-start baseline, and the "
                            f"model did real work -- accepting (BASELINE-BROKEN: verify was already "
                            f"failing before this run, likely an unprovisioned worktree).")
                        result = ("ACCEPTED: your changes introduced no new verify failures. The "
                                  "verify command still reports failures, but they were ALL "
                                  "present before your work began (a broken verify baseline, not "
                                  "your responsibility) -- so your task is accepted as complete.")
                        converged = True
                        final_summary = args.get("summary", "")
                    elif new_failures:
                        log(f"[worker] task_complete claim {completion_claims}/"
                            f"{MAX_COMPLETION_CLAIMS}: verify FAILED with "
                            f"{len(new_failures)} NEW failure(s) from this diff -- feeding only "
                            f"those back instead of the full output.")
                        result = ("NOT ACCEPTED: " + new_failure_feedback(new_failures) +
                                  "\nFix them, then call task_complete again. Do not run the "
                                  "verify command yourself -- the harness already did.")
                    else:
                        # Couldn't classify as no-regression: either no baseline (None/legacy or a
                        # passing baseline where ANY failure is new), we recognized none of the
                        # current failing output, or there was no evidence of work. Fall back to the
                        # original raw-output feedback rather than false-accept (Fable condition 1).
                        log(f"[worker] task_complete claim {completion_claims}/"
                            f"{MAX_COMPLETION_CLAIMS}: verify FAILED and could not be attributed to "
                            f"the baseline (unrecognized failure lines, no baseline, or no evidence "
                            f"of work) -- feeding the raw output back.")
                        result = (f"NOT ACCEPTED: the task's verification command still fails.\n"
                                  f"--- verify output ---\n{v_out[-3000:]}\n--- end ---\nFix the "
                                  f"reported problem(s), then call task_complete again. Do not run "
                                  f"the verify command yourself -- the harness already did.")
                elif verify and _baseline_is_the_task:
                    # Batch #7: this is where the pause now lives. The claim cap is reached,
                    # so the model has had MAX_COMPLETION_CLAIMS attempts with the full verify
                    # output in hand and still cannot make it pass. Accepting the model's word
                    # (what the generic cap branch below does) is unsound here for the same
                    # reason the in-loop accept was: the verify was failing before this run, so
                    # its continued failure is exactly what an untouched bug looks like. Two
                    # readings -- the fix did not land, or the verify's environment is broken --
                    # and neither is evidence of completion. Now the iterations really are
                    # spent, so a human is the right next step.
                    log(f"[worker] task_complete claim {completion_claims}: claim cap reached and "
                        f"the verify -- already failing at stage/enqueue -- still fails. It cannot "
                        f"distinguish a completed fix from an untouched bug (BASELINE-BROKEN). "
                        f"Pausing for review instead of accepting the model's word.")
                    # DISTINCT reason code on purpose. ollama-queue.py's auto-resume watchdog
                    # only ever auto-bumps "context_threshold" and "request_more_iterations";
                    # anything else it refuses to touch. A reason that fell into either bucket
                    # would be silently re-queued with more iterations and pause again -- an
                    # infinite loop burning GPU on a verify that cannot answer the question.
                    # This code makes the pause terminal until a human acts.
                    pause_reason_code = "verify_uninformative"
                    paused_for_review = (
                        "verify was already failing at stage/enqueue and still fails after "
                        f"{MAX_COMPLETION_CLAIMS} completion claims: it cannot show whether the "
                        "work landed. Either the fix did not take, or the verify's baseline is "
                        "broken. Fix the baseline, or check the change by hand.")
                    result = ("NOT ACCEPTED: the verify still fails, and it was already failing "
                              "before your work began -- so it cannot show whether your change "
                              "worked. This run is paused for a human to look at.")
                    converged = False
                else:
                    _cap_new = []
                    if verify:
                        _cv_ok, _cv_new, _, _, _ = _verify_delta_feedback(
                            verify, cwd, _baseline_verify_sig)
                        _cap_new = ([] if _cv_ok else
                                    attributable_new_failures(_cv_new, _baseline_verify_sig))
                    if _cap_new:
                        # Stop-early guard: past the claim cap, but the verify still reports
                        # failures THIS diff introduced -- the end-of-run verify will fail on
                        # them, so accepting only ends the run early. Only max_iters ends it.
                        log(f"[worker] task_complete claim {completion_claims}: claim cap reached "
                            f"but {len(_cap_new)} failure(s) introduced by this diff remain -- "
                            f"NOT accepting; the run continues until they are fixed or max_iters.")
                        result = ("NOT ACCEPTED: " + new_failure_feedback(_cap_new) +
                                  "\nFix them, then call task_complete again.")
                    else:
                        log(f"[worker] task_complete called (claim {completion_claims}) -- "
                            f"{'no --verify given' if not verify else 'claim cap reached'}, "
                            f"accepting the model's word.")
                        result = "ACCEPTED."
                        converged = True
                        final_summary = args.get("summary", "")
                # STOP-GATE (worker_robust, oh-my-agent persistent-mode / Tianshu deliver-task):
                # whatever accepted the claim above (verify pass, baseline-broken accept, no
                # verify at all), "done" additionally requires the spec's REQUIRED FILES to exist
                # and its MUST-CONTAIN literals to be present. ALL criteria are re-checked on
                # every claim, not only the previously failing ones.
                if (converged and _STOP_GATE_MAX > 0 and (_sg_files or _sg_lits_raw)
                        and os.environ.get("WORKER_STOP_GATE", "1") != "0"):
                    try:
                        _sg_changed = _git_changed_paths(cwd)
                    except Exception:
                        _sg_changed = []
                    _sg_ok, _sg_fails = _wr.check_stop_criteria(
                        cwd, _sg_files, _sg_lits_raw, verify_ok=None, extra_files=_sg_changed)
                    if not _sg_ok:
                        _stop_gate_fails += 1
                        _dispatch_metrics["stop_gate_refusals"] = _stop_gate_fails
                        converged = False
                        final_summary = None
                        result = _wr.stop_gate_message(_sg_fails, _stop_gate_fails, _STOP_GATE_MAX)
                        log(f"[worker] STOP-GATE refused task_complete ({_stop_gate_fails}/"
                            f"{_STOP_GATE_MAX}): {_sg_fails}")
                        if _stop_gate_fails >= _STOP_GATE_MAX:
                            _stop_gate_exhausted = True
            elif _lg_block is not None:
                result = _lg_block
                loop_break_notes.append(
                    "Your repeated web_search is a loop: the answer is in this repository. Use read_file / grep on it.")
            elif _thrash_cached is not None:
                # Anti-thrash: identical read-only call we already ran this run. Serve the
                # cached result annotated instead of re-running the tool (see
                # _anti_thrash_intercept). The stronger nudge, once past the threshold, is
                # delivered via loop_break_notes with the rest of this turn's guidance.
                result = _thrash_cached
                if _thrash_nudge:
                    loop_break_notes.append(_thrash_nudge)
                log(f"[worker] anti-thrash: served {name} from cache "
                    f"(repeat #{_thrash_repeats.get(sig)}) instead of re-running.")
            elif impl is None:
                result = f"ERROR: unknown tool {name}"
            else:
                try:
                    result = impl(cwd, args)
                except Exception as e:
                    result = f"ERROR: {e}"
            if name == "web_search" and _lg_prefix and isinstance(result, str) and not result.startswith("ERROR"):
                result = _lg_prefix + result
            if name != "task_complete":
                _lg_nudge = _lguards.after_tool(name, result, _is_own_verify)
                if _lg_nudge and isinstance(result, str):
                    result += _lg_nudge
            _dispatch_metrics["local_guard_events"] = dict(_lguards.events)
            # Same-page re-read nudge: appended to a SUCCESSFUL read, since the
            # loop this catches never produces an error to hang a warning on.
            if (name == "read_file" and isinstance(args, dict) and isinstance(result, str)
                    and _repeat_reads.get((str(args.get("path")), str(args.get("offset") or 1)), 0) >= 3):
                _m = re.search(r"lines (\d+)-(\d+) of (\d+)", result[:400])
                if _m and int(_m.group(2)) < int(_m.group(3)):
                    _nxt = int(_m.group(2)) + 1
                    result += (f"\n\n[NOTE: you have now read this exact page of "
                               f"{args.get('path')} 3+ times and it will not change. The rest of "
                               f"the file is further down -- call read_file with "
                               f'{{"path":"{args.get("path")}","offset":{_nxt},"length":300}} '
                               f"to advance, or use run_bash with grep to find a specific line "
                               f"number first and read around that offset.]")
            # Past-EOF read counter (all task kinds): the refusal itself is in _paginate_read; here the
            # consecutive occurrences are counted toward the named stop (read_past_eof_loop).
            try:
                _eof_v = _fan_guard.on_call(name, _wr.is_past_eof_result(result))
            except Exception:
                _eof_v = None
            if _eof_v:
                _dispatch_metrics["read_past_eof_streak"] = _fan_guard.eof_streak
                if _eof_v[0] == "warn":
                    loop_break_notes.append(_eof_v[2])
                elif _fan_abort is None or _fan_abort[0] == _wr.REASON_TOOL_FANOUT:
                    _fan_abort = (_eof_v[1], _eof_v[2])    # the more specific label wins
            # Tell the model what it sent that the tool does not accept. Appended
            # AFTER the call so the result is unchanged when nothing is unknown,
            # and so a stray key never costs the call itself.
            if _unknown and isinstance(result, str) and not result.startswith("REFUSED"):
                _decl = sorted(_tool_arg_names(name))
                result += (f"\n\n[NOTE: {name} does not take "
                           f"{', '.join(repr(u) for u in _unknown)} -- "
                           f"{'that argument was' if len(_unknown) == 1 else 'those arguments were'} "
                           f"IGNORED, not applied. {name} accepts: {', '.join(_decl) or '(none)'}.]")
            # VERIFY_ON_EDIT (default OFF): append the compact verify verdict to a successful edit's result.
            if (_wl is not None and name in ("write_file", "edit_file") and isinstance(result, str)
                    and isinstance(args, dict)):
                result += _wl.after_edit(cwd, verify, args.get("path"), result, _quick_verify,
                                         task_kind=task_kind, log=log)
            # Vacuous-verify-pass tracking (see the counters' own init comment above).
            if isinstance(result, str):
                if name in ("write_file", "edit_file") and result.startswith("OK: "):
                    _files_modified_count += 1
                elif name == "run_bash" and result.startswith('{"exit_code"'):
                    _run_bash_success_count += 1
            if _bp is not None:
                try:
                    _ro = (not _is_own_verify) and (
                        name in ("read_file", "list_files", "web_search", "web_fetch")
                        or (name == "run_bash" and isinstance(args, dict)
                            and _command_is_local_read(str(args.get("command", "")))))
                    _bp.observe_tool(i, name, args if isinstance(args, dict) else {}, result,
                                     own_verify=_is_own_verify, readonly=_ro, msg_index=len(messages))
                except Exception as _bo_err:
                    log(f"[worker] budget observe error (ignored): {_bo_err!r}")
            # Anti-thrash cache invalidation: a successful write, or a run_bash that is
            # not a pure local read, may have changed the tree, so every cached read
            # result is now stale. Without this a `read_file X` re-issued after editing
            # X was served the PRE-EDIT content annotated "unchanged -- act on it".
            if isinstance(result, str) and not (result.startswith("ERROR") or result.startswith("REFUSED")):
                if name in ("write_file", "edit_file", "str_replace") or (
                        name == "run_bash" and not _command_is_local_read(
                            str(args.get("command", "")) if isinstance(args, dict) else "")):
                    _tool_result_cache.clear()
            # WRITE-SIDE THRASH COUNTING (see write_thrash_key above): only SUCCESSFUL
            # writes feed the counter -- an ERROR/REFUSED result is a failed attempt and
            # must stay retryable, so it never counts. Identical bytes to the same path
            # 3+ times, or any scaffold-sentinel re-emission (collapsed per-path), trips
            # the EARLY ABORT at the top of the next turn; advancing writes change key.
            if (name in ("write_file", "edit_file", "str_replace")
                    and isinstance(result, str) and result.startswith("OK: ")
                    and isinstance(args, dict)):
                _wk = write_thrash_key(str(args.get("path")), json.dumps(args, sort_keys=True))
                _write_repeats[_wk] = _write_repeats.get(_wk, 0) + 1
            args_preview = json.dumps(args)[:200]
            result_preview = str(result)[:300]
            log(f"[worker] tool {name}({args_preview}) -> {result_preview}")
            if live is not None:
                live.tool_result(name, result)

            # Loop detection. Confirmed live 2026-08-22 (qwen2.5-coder:7b,
            # photo-upload): the model burned ALL 30 iterations repeating
            #   read_file app/components/ProfitCard.tsx -> not found
            #   list_files app/components/            -> not found
            # over and over. `components/` is at the repo root, not under
            # `app/` -- it had that fact from its own earlier listing and
            # never re-oriented. Nothing intervened, so a single wrong guess
            # became a total loss (0 files written). A prior run of the SAME
            # model on the SAME task wrote 5 correct files; the difference in
            # outcome was one bad turn with no recovery, which is why the
            # run-to-run variance looked so implausibly large.
            failed = str(result).startswith("ERROR") or str(result).startswith("REFUSED")
            # Anti-thrash cache store: remember a FRESH, successful read-only result so a
            # later identical call is served from here (see _anti_thrash_intercept). Only
            # fresh runs (_thrash_cached is None) so a cache hit never re-stores its own
            # annotated copy, and never a failure (a transient error should be retryable).
            if _thrash_cacheable and _thrash_cached is None and not failed:
                _tool_result_cache[sig] = result
            if name == "web_fetch" and not failed:
                web_fetch_succeeded = True
                web_fetch_success_count += 1
                real_fetched_texts.append(str(result))
            # Local-source verification ledger (see local_read_count's init comment):
            # a successful read_file/list_files, or a run_bash that reads/searches the
            # tree (grep/cat/find/git log/...), is genuine local verification. Pulled
            # from the SAME tool-call ledger web_fetch uses, one place that already
            # knows the tool name, success/failure, and args for this exact call.
            if not failed:
                if name in ("read_file", "list_files"):
                    local_read_count += 1
                elif name == "run_bash" and _command_is_local_read(
                        str(args.get("command", "")) if isinstance(args, dict) else ""):
                    local_read_count += 1
            if failed:
                repeated_failures[sig] = repeated_failures.get(sig, 0) + 1
                if repeated_failures[sig] in (3, 6, 9):
                    log(f"[worker] loop-break: {name} has failed 3x with identical args -- injecting corrective guidance.")
                    # Added 2026-08-28: this used to hardcode file-path advice
                    # ("call list_files... find where the file actually
                    # lives") unconditionally -- wrong, confusing guidance if
                    # the tool that's actually looping is web_search/web_fetch
                    # (e.g. retrying the same dead URL 3 times), where the
                    # right advice is "try a different query/URL", not
                    # "list_files". Same underlying pattern as the
                    # coding-biased corrective nudge and system prompt fixed
                    # earlier tonight -- adapt the guidance to which tool is
                    # actually stuck instead of assuming it's always a file path.
                    if name in ("read_file", "list_files", "write_file", "edit_file"):
                        specific = ("That path does not exist. Do NOT call it again. Call list_files "
                                    "on '.' and on the parent directory to find where the file actually "
                                    "lives, and use only paths you have seen in a list_files result.")
                    elif name in ("web_search", "web_fetch"):
                        specific = ("That query/URL is not working. Do NOT call it again with the same "
                                    "arguments. Try a different, more specific search query, or a "
                                    "different source URL entirely.")
                    else:
                        specific = "Do NOT call it again with the same arguments. Try a different approach."
                    loop_break_notes.append(
                        f"You have now called {name} with exactly these arguments 3 times and it has "
                        f"failed every time: {args_preview}. {specific}"
                    )
            else:
                repeated_failures.pop(sig, None)
            # Confirmed live 2026-08-28 (llama3.1:8b, EV-charging discovery v5): the loop-break
            # guard above only counts FAILING calls, so a tool call that "succeeds" every time
            # but returns the same unhelpful result (a web_search whose top hit is irrelevant,
            # e.g.) never trips it -- this run repeated the identical web_search query for all
            # 22/22 iterations, burned the entire budget, and never converged. Track identical
            # calls regardless of success/failure and nudge (soft, not a hard REFUSE -- a
            # search that keeps "succeeding" isn't a dead path the way a 404 is) once repetition
            # itself is the problem.
            # EXEMPT THE JOB'S OWN VERIFY (2026-09-02). Re-running `bash verify.sh`
            # after every edit is exactly what a converge-on-verify task INSTRUCTS,
            # and it is the progress signal, not a loop. Confirmed live during the
            # coding bake-off: loop-detect fired "run_bash called 3x with identical
            # arguments, none of them advancing the task" where the identical
            # command WAS the verify -- on a run that was converging and went on to
            # pass 8/0. Counting it as repetition pathologises correct behaviour for
            # every model on every dispatch, and it briefly made a converging model
            # look like it was stuck in a loop. (_is_own_verify was computed once, up where
            # the anti-thrash intercept needs it -- reused here for the loop-detect exemption.)
            if _is_own_verify:
                repeated_calls.pop(sig, None)
            else:
                repeated_calls[sig] = repeated_calls.get(sig, 0) + 1
            if not _is_own_verify and not failed and repeated_calls[sig] in (3, 6, 9):
                log(f"[worker] loop-detect: {name} called {repeated_calls[sig]}x with identical "
                    f"arguments, none of them advancing the task -- injecting corrective guidance.")
                loop_break_notes.append(
                    f"You've now called {name} with the exact same arguments "
                    f"{repeated_calls[sig]} times, and it keeps returning the same result without "
                    f"moving the task forward. Repeating it again will not help -- try a "
                    f"genuinely different query, URL, or approach instead. If the task is "
                    f"actually finished, call task_complete instead of repeating this."
                )
            if failed:
                repeated_calls.pop(sig, None)
            try:
                _loop_det.observe(name, args if isinstance(args, dict) else {}, exempt=_is_own_verify,
                                  error=(str(result) if failed else None))
            except Exception as _ld_err:     # telemetry/guard must never break a dispatch
                log(f"[worker] loop-detector observe error (ignored): {_ld_err!r}")
            if manual_call_this_turn:
                # role:"tool" combined with omitting the native `tools` API
                # field is an untested combination for this model -- a
                # plain user-role result message is what was actually
                # proven to work end-to-end live 2026-08-21, so stick with
                # that instead of assuming role:"tool" also works here.
                messages.append({"role": "user", "content": f"[tool result for {name}]: {result}"})
            elif api_style == "openai":
                # OpenAI-compatible tool-result messages are keyed back to
                # their call via tool_call_id -- confirmed required (tested
                # live against llama-server 2026-08-22) for the model to
                # correctly associate the result with its own call.
                tool_msg = {"role": "tool", "content": str(result)}
                if tool_call_id:
                    tool_msg["tool_call_id"] = tool_call_id
                messages.append(tool_msg)
            else:
                messages.append({"role": "tool", "content": str(result)})

        if converged:
            # task_complete was accepted above -- stop immediately rather than sending loop-
            # break guidance or a budget note for an iteration that will never happen. The
            # final _save_transcript below (after the outer loop) records this correctly.
            break

        # FROZEN-REASONING HARD STOP. Taken here, after this turn's tool calls were
        # processed, so a turn that also converged or paused is never pre-empted by it.
        # Unlike the read/write thrash aborts above this deliberately does NOT require an
        # unchanged working tree: a model repeating its own reasoning verbatim has stopped
        # deciding anything, and whether it happened to touch a file along the way does not
        # make the next iteration any more likely to differ. See harvest_reasoning below
        # for what the run leaves behind instead of nothing.
        if _reasoning_frozen:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: the model's reasoning "
                f"has been near-identical for {_reasoning_streak + 1} consecutive turns "
                f"(>= {REASONING_FREEZE_SIMILARITY:.2f} normalised similarity) -- a decode "
                f"fixed point, not progress. Corrective guidance was already delivered and "
                f"did not change it. Stopping to save GPU rather than grinding to max_iters; "
                f"the last reasoning block is harvested as a partial answer below.")
            _dispatch_metrics["early_abort"] = "reasoning_freeze"
            break

        # STOP-GATE exhausted (worker_robust.check_stop_criteria): the model claimed done
        # _STOP_GATE_MAX times while required files / must-contain literals were still missing.
        if _stop_gate_exhausted:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: task_complete refused "
                f"{_stop_gate_fails} times by the stop-gate (required file / must-contain literal "
                f"still missing) -- {_wr.REASON_STOPGATE}.")
            _dispatch_metrics["early_abort"] = _wr.REASON_STOPGATE
            break

        # PER-TURN FAN-OUT / PAST-EOF STOP (worker_robust.TurnFanoutGuard), every task kind. Checked
        # BEFORE the loop detector so a runaway turn is labelled specifically, not as a generic nav_loop.
        if _fan_abort and not paused_for_review:
            log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_fan_abort[1]} -- {_fan_abort[0]}. "
                f"Stopping instead of burning iterations; the scheduler should re-sample / re-spec, "
                f"not blind-retry.")
            _dispatch_metrics["early_abort"] = _fan_abort[0]
            break

        # LOOP DETECTOR (worker_robust.LoopDetector): repeated identical reads / writes, A->B->A
        # write thrash, N iterations with nothing new. First detection injects ONE corrective
        # message; the next ends the run with a named reason so the scheduler re-specs.
        try:
            _ld = _loop_det.end_iteration(i)
        except Exception as _ld_err:
            _ld = None
            log(f"[worker] loop-detector error (ignored): {_ld_err!r}")
        if _ld and not paused_for_review:
            _ld_action, _ld_kind, _ld_msg = _ld
            _dispatch_metrics["loop_detector"] = {"kind": _ld_kind, "action": _ld_action, "iteration": i}
            if _ld_action == "warn":
                log(f"[worker] loop-detector WARN ({_ld_kind}) at iteration {i}: {_ld_msg}")
                loop_break_notes.append(_ld_msg)
            else:
                _ld_reason = _wr.loop_exit_reason(_ld_kind)
                log(f"[worker] EARLY ABORT at iteration {i}/{total_iters}: {_ld_msg} -- "
                    f"{_ld_reason} ({_ld_kind}). Stopping instead of burning GPU time; the "
                    f"scheduler should re-spec this task.")
                _dispatch_metrics["early_abort"] = _ld_reason
                break

        if not paused_for_review:
            if _bp is not None:
                try:
                    _bpr = _bp.end_turn(i, total_iters, cwd)
                    if _bpr["extend"]:
                        total_iters += _bpr["extend"]
                        log(f"[worker] BUDGET POLICY progress_extend: +{_bpr['extend']} iterations -> "
                            f"ceiling {total_iters}.")
                    if _bpr.get("reverted"):
                        _tool_result_cache.clear()
                        log(f"[worker] BUDGET POLICY checkpoint_revert: restored {_bpr['reverted']} to the best verify state.")
                    for _bn in _bpr["notes"]:
                        messages.append({"role": "user", "content": _bn})
                    if _bpr["budget_line"]:
                        messages.append({"role": "user", "content": _bpr["budget_line"]})
                    _dispatch_metrics.update(_bp.metrics())
                except Exception as _be_err:
                    log(f"[worker] budget end-of-turn error (ignored): {_be_err!r}")
            remaining = total_iters - i
            # Proportional, not a flat 3 (2026-09-22). With a 3-iteration floor the
            # warning arrived far too late to be actionable: request_more_iterations
            # PAUSES for review, so a model told at 27/30 that it needs more room has
            # to both notice and act inside the last three turns, while head-down in a
            # debug loop. Across the 60 most recent transcripts exactly ONE job of the
            # 26 that ran out of budget ever called it. Firing at 25% remaining (still
            # never below 3) gives the model real room to recognise it is short.
            _low_budget_at = max(ITERATION_LOW_BUDGET_THRESHOLD, total_iters // 4)
            if remaining > 0 and remaining <= _low_budget_at:
                messages.append({
                    "role": "user",
                    "content": f"[Iteration budget warning: you are on iteration {i} of "
                               f"{total_iters} -- only {remaining} iteration(s) remain before "
                               f"this dispatch stops with DID-NOT-CONVERGE if the task isn't "
                               f"done. If you have concrete remaining work that will not fit "
                               f"in those, call request_more_iterations now (it pauses for "
                               f"review rather than granting anything immediately). Only do "
                               f"that for real remaining work -- not as a routine check-in, "
                               f"and not to recover from being stuck. If the task is actually "
                               f"complete, call task_complete now instead of calling another "
                               f"tool just to keep going.]",
                })
            elif remaining > 0 and i % 3 == 0 and not (_bp is not None and _bp.on("budget_visible")):
                # Fable finding 2026-08-29: this note used to fire every single iteration,
                # addressing the model directly right after every turn including ones where it
                # had just given a would-be-final answer -- a real transcript showed a model
                # settle into repeating a summary + a no-op "echo done" tool call 18 times in a
                # row, plausibly because being re-addressed after each "final" turn reads as a
                # prompt to keep acting. Every 3rd iteration is enough to keep the model aware
                # of its budget without constant "act now" pressure; the low-budget variant
                # above still fires every iteration since urgency is warranted there.
                messages.append({
                    "role": "user",
                    "content": f"[Iteration {i}/{total_iters}: {remaining} remaining in your budget.]",
                })

        # Deliver any loop-break guidance accumulated this turn, as a plain user message
        # so it reaches models with no tool-role template.
        #
        # ORDER IS LOAD-BEARING (2026-09-19). This used to run BEFORE the budget note,
        # which meant that on every third iteration the corrective ended up sandwiched:
        # the LAST thing the model saw was "[Iteration 15/31: 16 remaining...]", not "stop
        # repeating yourself". The BFMR read_thrash transcript shows exactly that at
        # messages [40][41] -- guidance, then a budget ping, then the model repeated the
        # call again. Corrective guidance is the highest-priority thing in the turn and
        # goes last, in the slot models actually attend to; the budget note is ambient.
        if loop_break_notes:
            messages.append({"role": "user", "content": "\n\n".join(loop_break_notes)})
            loop_break_notes = []

        # Incremental save after this iteration's tool execution completes:
        # a kill at any point loses at most this one iteration's work, not
        # the whole run -- see _save_transcript's docstring and the --resume
        # support above for why this exists. converged=False here because the
        # real value isn't known until the loop ends; the final save below
        # overwrites it with the real one.
        _save_transcript(log_path, model, host, cwd, task, False, i, messages,
                          worktree_start_snapshot=_worktree_start_snapshot,
                          baseline_verify_sig=_baseline_verify_sig)
        # Bug #1 (2026-09-18): emit a greppable CHECKPOINT marker after EACH incremental
        # save so a job KILLED mid-run (the queue daemon's bounded SIGTERM->SIGKILL
        # preemption escalation) is still resumable from its last completed iteration.
        # The end-of-run RESUMABLE TRANSCRIPT marker (below) is only reached on a clean
        # loop exit, so a SIGKILL'd job would otherwise have no marker and relaunch from
        # scratch, losing every prior iteration. log_path is stable across the run, so
        # the daemon's last-wins parse always points at the freshest transcript.
        log(f"[worker] CHECKPOINT TRANSCRIPT: {log_path}")

        # Context-usage counterpart to request_more_iterations (the owner's request, "can we do
        # the same for context?"): a model can't self-report running low on context the way
        # it can ask for more iterations, since it doesn't see its own token accounting --
        # so this is harness-driven instead, checked against the objective usage the API
        # already returned for this iteration. Moved HERE (was checked immediately on
        # receiving the response, before tool_calls were even read) after Fable found this
        # was silently dropping a pending tool call: the model's response for this iteration
        # could legitimately include a tool call, but breaking before line ~2802 ever looked
        # at tool_calls meant it was appended to `messages` and then simply never executed or
        # answered -- on resume, the model faced its own dangling, unanswered call and often
        # produced a blank response. Checking here instead, after this iteration's tool calls
        # have actually run and been saved, means a pause always lands on a fully completed
        # iteration, same as the SIGTERM check just below and the file's own documented rule
        # for `_save_transcript` (see its docstring).
        if not paused_for_review:
            total_tokens_used = usage.get("total_tokens") or 0
            if num_ctx and total_tokens_used >= num_ctx * CONTEXT_REVIEW_THRESHOLD:
                paused_for_review = (
                    f"context usage {total_tokens_used}/{num_ctx} tokens "
                    f"({total_tokens_used / num_ctx:.0%}) at or above the "
                    f"{CONTEXT_REVIEW_THRESHOLD:.0%} review threshold"
                )
                pause_reason_code = "context_threshold"
                pause_meta = {"tokens_used": total_tokens_used, "num_ctx": num_ctx}
                log(f"[worker] PAUSED FOR REVIEW: {paused_for_review}. To grant more room, "
                    f"resume with: --resume {log_path} --num-ctx <bigger> (in addition to the "
                    f"same other flags this dispatch used). To deny, just don't -- the "
                    f"transcript stays as-is.")
            elif num_ctx:
                # Proactive mid-run budget nudges BELOW the 0.90 pause (see
                # _context_budget_nudges): warn the model to converge while it still has
                # room, each threshold at most once per run. Only reached when the pause
                # above did not fire this iteration.
                for _thr, _msg in _context_budget_nudges(
                        total_tokens_used, num_ctx, _ctx_nudge_fired):
                    messages.append({"role": "user", "content": _msg})
                    log(f"[worker] context-budget nudge at {total_tokens_used / num_ctx:.0%} "
                        f"(threshold {_thr:.0%}) -- steering toward convergence.")

        # External pause (SIGTERM from the queue daemon's promote flow): honor it HERE -- at
        # the end of a fully completed and saved iteration -- so the transcript on disk is
        # always complete through some whole iteration. Reuses paused_for_review's existing
        # break/log/save path below exactly as request_more_iterations does; no separate stop
        # logic, and the exit code at the bottom reports EXIT_CODE_PAUSED for both pause
        # sources alike (resumable, not a failure).
        if _sigterm_pause_requested:
            paused_for_review = "external pause (SIGTERM) from queue daemon"
            pause_reason_code = "external_sigterm"
            pause_meta = {}

        # request_more_iterations sets this from inside the tool-call loop above (a `break`
        # there only exits that inner loop) -- check it here, after this iteration's transcript
        # save, so the pause is captured before the outer loop actually stops.
        if paused_for_review:
            break

    if not converged:
        if paused_for_review:
            log(f"[worker] PAUSED FOR REVIEW at iteration {i}/{total_iters}: {paused_for_review}")
        else:
            log(f"[worker] DID NOT CONVERGE after {total_iters} iterations -- stopping, output may be incomplete.")
            # HARVEST-ON-ABORT (2026-09-19). A thrash/freeze abort used to leave literally
            # nothing: the BFMR diagnosis run spoke only through tool calls, so every
            # assistant `content` was "" and ollama-queue.py's _extract_final_answer found
            # no answer to persist -- while the reasoning block the run died holding was a
            # real partial diagnosis. Recover it as a clearly-labelled assistant message so
            # the existing answer pipeline picks it up. Strictly additive: it only fires when
            # the run's FINAL assistant turn said nothing (see run_ended_without_answer for
            # why "final" and not "any"), so a run that ended on a real written answer is
            # never touched, and `converged` / `terminal_reason` are unchanged -- the run
            # still FAILED, it just stopped failing empty-handed.
            _ea_harvest = _dispatch_metrics.get("early_abort")
            if (_ea_harvest in ("reasoning_freeze", "thrash_zero_diff", "write_thrash", "output_cap_loop",
                                "prose_loop", "wall_budget") + tuple(_wr.LOOP_KIND_REASONS.values())
                    and run_ended_without_answer(messages)):
                _harvested = harvest_reasoning(messages)
                if _harvested:
                    content = build_harvested_answer(_ea_harvest, _harvested)
                    messages.append({"role": "assistant", "content": content})
                    log(f"[worker] harvested the last reasoning block ({len(_harvested)} chars) "
                        f"as a labelled PARTIAL answer -- the run produced no assistant text of "
                        f"its own and would otherwise have written no answer at all.")
                    _dispatch_metrics["harvested_reasoning_chars"] = len(_harvested)
                else:
                    log("[worker] nothing to harvest: no assistant text and no substantive "
                        "reasoning block in the transcript.")
            # Confirmed live 2026-08-28 (EVgo research): the warning banner above only fires on
            # the clean "no tool calls, accepted" exit -- a model that gets nudged for zero real
            # fetches and then simply runs out of iteration budget mid-nudge-cycle (a real
            # DID-NOT-CONVERGE, not a paused-for-review) skips that check entirely, so its last
            # message can carry confidently-stated fabricated numbers with NO warning attached
            # at all. Apply the same unconditional banner here, to whichever message is actually
            # the last assistant turn (that's what a reader sees as "the answer"), covering this
            # exit path too.
            if should_stamp_unverified(task_kind, web_fetch_succeeded, local_read_count,
                                       facts_provided):
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        m["content"] = (m.get("content") or "") + (
                            "\n\n---\nHARNESS WARNING: this dispatch ran out of iterations "
                            "without ever completing a verified answer, and no web_fetch call "
                            "succeeded anywhere in this session. Any claim above of having "
                            "'verified' or 'confirmed' a detail is NOT reliable, regardless of "
                            "what the text above says -- treat every specific fact in this "
                            "answer as unconfirmed until it is checked against a source that "
                            "was actually, successfully fetched."
                        )
                        log("[worker] research task hit DID-NOT-CONVERGE with zero successful "
                            "web_fetch calls -- appending the same unconditional harness "
                            "warning to the last assistant message.")
                        break

    # Final save via the same helper as the incremental per-iteration saves
    # above, with the real converged value. log_path is NOT reassigned here:
    # on a fresh run it's the new timestamped file created up top, and on a
    # resumed run it's the resume file itself, which keeps getting updated
    # across pause/resume cycles.
    _save_transcript(log_path, model, host, cwd, task, converged, i, messages,
                      pause_reason=pause_reason_code, pause_meta=pause_meta,
                      worktree_start_snapshot=_worktree_start_snapshot,
                      baseline_verify_sig=_baseline_verify_sig)
    log(f"[worker] full transcript written to {log_path}")
    if paused_for_review:
        # Single greppable marker for the queue daemon (ollama-queue.py parses this out of
        # the job's log file to learn which transcript to --resume from when it relaunches a
        # paused job). Printed for BOTH pause sources -- external SIGTERM and the model's own
        # review gates -- since both leave an identically resumable transcript.
        log(f"[worker] RESUMABLE TRANSCRIPT: {log_path}")

    if live is not None:
        if converged:
            live.result_box(final_summary or content or "(no final text)", True, i)
        elif paused_for_review:
            # The iteration the run ACTUALLY stopped at (i), not the budget (total_iters):
            # a pause at 5/20 left 15 iterations unspent, and that distinction is the whole
            # point of the pause. The plain log() above already reports it this way -- this
            # is the live/dashboard view catching up.
            live.result_box(f"PAUSED at iteration {i}/{total_iters} -- {paused_for_review}",
                            False, i, status="paused")
        else:
            live.result_box(f"DID NOT CONVERGE after {total_iters} iterations", False, total_iters)

    # Auto-capture fallback (Fable ruling 2026-08-30): some models -- nemotron-cascade-2
    # especially -- put a review-style deliverable in their final TEXT answer instead of
    # writing the file, despite the task instruction and the bounded verify-nudges. When
    # --capture-final-as names a file the run never produced, write the final text answer
    # to it and log `capture=fallback`, so the content is SCORED rather than lost as a
    # NO_REVIEW cell. Runs BEFORE verify (so a `[ -f REVIEW.md ]` verify then passes) and
    # only for a completed (non-paused) run -- a paused run's output is incomplete by
    # definition. Report the fallback rate separately (grep logs for "capture=fallback").
    capture_fallback = False
    if capture_final_as:
        _cap = resolve_path(cwd, capture_final_as)
        _should, _text, _tag = _capture_decision(
            capture_final_as, _cap.exists(), paused_for_review, (final_summary or content or ""))
        if _rev_bounds and _rev_abort and not converged and not _cap.exists():
            # a bounded review stopped by a worker bound: the captured file is the deterministic
            # REVIEW CAPPED marker (never the runaway's partial text).
            _should, _tag = True, "capture=review-capped"
            _text = _wr.review_capped_text(_rev_abort[0], _rev_abort[1], i)
        if _should:
            try:
                _cap.write_text(_text)
                capture_fallback = True
                log(f"[worker] {_tag}: saved final answer to {capture_final_as} ({len(_text)} chars).")
            except Exception as _e:
                log(f"[worker] capture-final-as write failed for {capture_final_as}: {_e}")

    verify_passed = None  # None = not run, distinct from False = ran and failed
    # True only when the end-of-run verify FAILED yet every failure pre-existed the dispatch
    # (0 new failures from the model's diff). Kept separate from verify_passed so the vault
    # log can say "no regression" rather than a bare PASSED, while the exit code still treats
    # it as not-a-failure. Added 2026-08-30 (baseline-delta).
    _baseline_no_regression = False
    if verify and paused_for_review:
        # A paused run is by definition incomplete -- running its verify command against
        # half-finished output would just produce a misleading failure (and could take up to
        # 300s), so it's skipped; the exit code below reports "paused", not "verify failed".
        log("[worker] skipping verify command: run is paused, output is incomplete by definition.")
    elif verify:
        log(f"[worker] running verify command: {verify}")
        # Fable code review, 2026-08-29: this subprocess.run had NO timeout handling
        # anywhere up the call stack (run_task is invoked bare via sys.exit(run_task(...))
        # at the bottom of this file) -- a verify command hanging past 300s raised
        # TimeoutExpired and killed the whole process with a traceback AFTER all the
        # model's real work: no VERIFY line logged, the vault log below never ran despite
        # being documented as unconditional, no keep_alive/evict, live.close() skipped.
        # A real crash bug, not a hypothetical one -- fixed in the same change as the
        # vacuous-pass guard below since Fable flagged it four lines from this edit.
        # Snapshot taken BEFORE running verify, not after (Fable ruling) -- a verify
        # command that builds artifacts (compiles, generates a lockfile, etc.) would
        # dirty the tree itself and fake a non-empty diff if taken afterward. None
        # when cwd isn't a git repo at all, same as _worktree_start_snapshot.
        _worktree_end_snapshot = (_git_worktree_snapshot(cwd)
                                   if _worktree_start_snapshot is not None else None)
        try:
            _vrc, _vout, _verr = _run_shell_group(verify, cwd, 300)
        except _GroupTimeout as _vt:
            log("[worker] VERIFY TIMED OUT (300s) -- treating as failed. The task's own work "
                "may well be fine; it's the verify COMMAND itself that didn't finish in time "
                "(a hanging build/test, a network call with no timeout of its own, etc.).")
            verify_passed = False
            _dispatch_metrics["verify_exit_code"] = None
            _dispatch_metrics["verify_output_tail"] = (
                "VERIFY TIMED OUT (300s)\n" + (_vt.stdout + "\n" + _vt.stderr)[-2000:])
        else:
            result = subprocess.CompletedProcess(verify, _vrc, _vout or "", _verr or "")
            # Persist the verify's own words: until now the end-of-run verify output
            # existed only as log() lines in a job log that is routinely gone by the
            # time anyone asks why a run failed (every recent nonconvergence had
            # log=None in queue state). dispatch-metrics.jsonl keeps the tail.
            _dispatch_metrics["verify_exit_code"] = result.returncode
            _dispatch_metrics["verify_output_tail"] = (
                (result.stdout or "") + (("\n" + result.stderr) if result.stderr else ""))[-2000:]
            log(f"[worker] verify stdout:\n{result.stdout}")
            if result.stderr:
                log(f"[worker] verify stderr:\n{result.stderr}")
            if result.returncode in (126, 127):
                # Distinct from a real assertion failure: 126/127 means the shell couldn't
                # even RUN the verify command (not found / not executable), not that it ran
                # and found something wrong. Conflating the two sends debugging down the
                # wrong path -- the fix here is to the --verify string, not the model's work.
                log(f"[worker] VERIFY FAILED (exit {result.returncode}) -- this looks like the "
                    f"verify COMMAND ITSELF is broken (not found / not executable), not a real "
                    f"assertion failure against the task's work. Check the --verify string.")
                verify_passed = False
            else:
                verify_passed = result.returncode == 0
                if verify_passed:
                    log("[worker] VERIFY PASSED (exit 0).")
                    # Vacuous-pass guard (Fable GO-with-conditions 2026-08-29, REVISED same
                    # day after github-projects-bf caught a real gap in the first version: a
                    # session that only explored via run_bash -- 9 successful calls, zero
                    # edits, zero diff -- landed in a WARN-only branch under a run_bash-
                    # success-count discriminator, because "run_bash succeeded" only proves a
                    # command RAN, not that it changed anything. Ground truth (did the tree
                    # actually change) supersedes inferring intent from tool-call counts.
                    # git-repo case: the diff-snapshot comparison is authoritative --
                    # write_file/edit_file AND heredoc/sed/git-apply-via-run_bash AND direct
                    # commits are all covered (rev-parse HEAD is part of the snapshot
                    # specifically so a commit-without-a-dirty-tree still counts as changed).
                    # Coding-only: task_kind=="research" has no file-based deliverable by
                    # design (see any_mutation_called's own comment above), so this guard
                    # doesn't apply there regardless of which branch below would otherwise fire.
                    # HARNESS SELF-CHECK EXEMPTION (2026-09-19, approved scope).
                    # An auto-author / auto-refine dispatch's deliverable is a STATE --
                    # "the harness discriminates and is satisfiable" -- not a diff. When
                    # the harness is already complete (a resumed authoring run, or a
                    # refine round enqueued for a reason that turned out not to exist),
                    # the correct model behaviour is to change NOTHING, and this guard
                    # then failed it for doing the right thing. Measured: 13 jobs have
                    # been failed by the guard, 10 of them (77%) exactly this way -- and
                    # a FAILED authoring slice is re-author-eligible, so each one seeds
                    # the retry storm MAX_AUTHOR_ATTEMPTS exists to cap.
                    #
                    # Deliberately keyed on the VERIFY'S OWN OUTPUT, not on the label or
                    # task_kind: only the authoring self-check emits this marker, so a
                    # coding dispatch can never claim the exemption, and a zero-edit
                    # vacuous pass on real code is still caught exactly as before. That
                    # is the case this must not loosen, and the revert-test covers it.
                    # ONE decision, made by zero_diff_verdict() so the behaviour that is
                    # unit- and revert-tested IS the behaviour that ships. This used to be
                    # inline; the exemption was added as a parallel branch and immediately
                    # created two records of one fact, which is precisely the drift this
                    # codebase keeps getting bitten by.
                    _is_git = _worktree_start_snapshot is not None
                    _tree_changed = (_is_git and
                                     _worktree_end_snapshot != _worktree_start_snapshot)
                    _zd = zero_diff_verdict(
                        task_kind, result.stdout, result.stderr,
                        tree_changed=_tree_changed,
                        verify_cmd=verify,
                        files_modified_count=_files_modified_count,
                        is_git_repo=_is_git,
                        run_bash_success_count=_run_bash_success_count)
                    if _zd == "exempt":
                        log("[worker] zero-diff ACCEPTED: the verify reports "
                            f"{HARNESS_SELFCHECK_MARKER!r}, so this dispatch's deliverable is "
                            "the harness STATE, not a diff -- an already-complete harness is "
                            "a correct outcome, not a vacuous pass. (The unchanged-tree guard "
                            "still applies to every verify that does NOT emit this marker.)")
                        # ...but "the deliverable is STATE, not a diff" cuts both ways: a
                        # diff left in the TARGET is residue this premise says should not
                        # be there, and shipping it poisons the next stage's baseline (see
                        # harness_exempt_revert_decision's comment for the live case).
                        _ht = declared_harness_target(cwd)
                        _do_revert, _why = harness_exempt_revert_decision(
                            _zd, _is_git, _tree_changed, _ht,
                            target_dirty_at_start=snapshot_path_dirty(
                                _worktree_start_snapshot, _ht),
                            target_dirty_now=_git_path_is_dirty(cwd, _ht) if _ht else None)
                        if _do_revert:
                            _ok, _err = _git_restore_path(cwd, _ht)
                            if _ok:
                                log(f"[worker] HARNESS-EXEMPT TARGET REVERTED: {_ht} was "
                                    "clean at dispatch start and is dirty now, on a run "
                                    "whose deliverable is the harness STATE -- almost "
                                    "certainly refimpl.py applied to the target to prove "
                                    "the fixture discriminates, left behind because it was "
                                    "applied outside auto-harness-check.py's try/finally "
                                    "revert(). Restored to HEAD before completing so the "
                                    "next stage's baseline-clean pre-flight does not NO-GO "
                                    "on it. (Greppable prefix HARNESS-EXEMPT TARGET "
                                    "REVERTED. Only the declared target is touched -- the "
                                    "fixture/TASK.md/refimpl.py edits that ARE this round's "
                                    "deliverable are left exactly as the model wrote them.)")
                            else:
                                log("[worker] HARNESS-EXEMPT TARGET REVERT FAILED for "
                                    f"{_ht}: {_err} -- leaving the tree as-is. The target is "
                                    "still dirty, so expect the next stage's baseline-clean "
                                    "pre-flight to NO-GO; that is the loud failure, and it is "
                                    "the right one to get.")
                        elif _why not in ("tree-unchanged", "target-already-clean"):
                            # Every other abstain is a case where residue MAY be sitting in
                            # the tree and this guard deliberately declined to touch it.
                            # Silence here is what made the live incident read as an
                            # unrelated "queue stuck" ticket.
                            log("[worker] harness-exempt target revert SKIPPED "
                                f"({_why}; target={_ht!r}) -- not reverting, because "
                                "destroying state this guard cannot prove it created is "
                                "worse than leaving residue for the next pre-flight to "
                                "catch. If the next stage NO-GOs on a dirty baseline, this "
                                "line is why.")
                    elif _zd == "fail" and _is_git:
                        verify_passed = False
                        log("[worker] OVERRIDING TO FAILED: verify passed but the working "
                            "tree is byte-for-byte unchanged since dispatch start (same "
                            "HEAD, same git status, same diff) AND zero write_file/edit_file "
                            "calls succeeded -- almost certainly a vacuous pass (e.g. a "
                            "verify command that only checks pre-existing file validity), "
                            "not real completed work.")
                    elif _zd == "fail":
                        verify_passed = False
                        log("[worker] OVERRIDING TO FAILED (degraded check -- cwd is not a "
                            "git repo, no working-tree diff available): verify passed but "
                            "ZERO files were modified and ZERO run_bash calls succeeded this "
                            "session -- almost certainly a vacuous pass, not real completed "
                            "work.")
                    elif _zd == "warn" and _is_git:
                        log("[worker] VACUOUS-WARN: verify passed and the git working tree "
                            "shows no change, but write_file/edit_file DID succeed this "
                            "session -- likely a write outside the repo or to a "
                            ".gitignore'd path (real work, just invisible to git), possibly "
                            "still a vacuous pass otherwise. Not auto-failing this one, but "
                            "worth a look before trusting it. (Greppable prefix VACUOUS-WARN "
                            "for downstream tooling -- WARN + exit 0 is otherwise invisible.)")
                    elif _zd == "warn":
                        log("[worker] VACUOUS-WARN: (degraded check -- cwd is not a git "
                            "repo) verify passed with zero write_file/edit_file calls, but "
                            "run_bash ran successfully this session -- legitimate if the "
                            "model made its edits via run_bash, still possibly a vacuous "
                            "pass otherwise. Not auto-failing this one, but worth a look.")
                else:
                    # Baseline-delta on the AUTHORITATIVE end-of-run verify (added 2026-08-30):
                    # if every current failure was already present at dispatch start, the model's
                    # diff introduced no regressions the verify can see. Reporting exit 1 (FAILED)
                    # here is what falsely failed correct work in the resell-tracker incident (a
                    # verify already broken by a missing `prisma generate`). Distinguish the two:
                    # a genuine NEW failure still FAILS; a no-new-failure run is marked
                    # BASELINE-BROKEN and allowed to pass (exit 0) so real work isn't discarded.
                    # Excludes the 126/127 branch above by construction (that's a broken verify
                    # COMMAND, handled separately). num_ctx/timeouts unaffected.
                    #
                    # A no-regression PASS here needs the SAME three soundness gates as the in-loop
                    # accept (Fable review 2026-08-30): (1) the baseline was genuinely non-empty
                    # (real pre-existing failures, not a passing baseline's empty set nor a
                    # None/legacy baseline); (2) we RECOGNIZED the current failing output (an empty
                    # current signature can't prove "no new failures" -- an unrecognized regression
                    # would otherwise pass); (3) evidence the model actually changed the tree, since
                    # setting verify_passed=True on a FAILING verify bypasses the exit-0-only
                    # vacuous-pass guard below -- without this a zero-edit run on a broken baseline
                    # would flip exit 1 -> 0. All three must hold, else this stays a real FAILURE.
                    _end_out = (result.stdout or "") + (
                        ("\n" + result.stderr) if result.stderr else "")
                    _end_cur = _verify_failure_signature(_end_out)
                    _end_new = (_end_cur - _baseline_verify_sig
                                 if _baseline_verify_sig is not None else None)
                    if _worktree_start_snapshot is not None:
                        _end_did_work = (_worktree_end_snapshot != _worktree_start_snapshot)
                    else:
                        _end_did_work = (_files_modified_count > 0)
                    # Same rule at the authoritative end-of-run verify: if the queue's
                    # pre-flight already saw this verify FAIL at enqueue, a still-failing
                    # verify proves nothing and must not be flipped to a pass.
                    if (_end_new is not None and not _end_new
                            and bool(_baseline_verify_sig) and _end_cur and _end_did_work
                            and not _baseline_is_the_task):
                        _baseline_no_regression = True
                        verify_passed = True  # authoritative: no recognized regression + real work
                        log(f"[worker] VERIFY FAILED (exit {result.returncode}) BUT every recognized "
                            f"failure pre-existed at dispatch start (0 new from this diff) and the "
                            f"model changed the tree -- marking BASELINE-BROKEN and NOT failing the "
                            f"run (verify baseline was already broken, e.g. an unprovisioned "
                            f"worktree / missing codegen). Fix the baseline before trusting this as "
                            f"a clean pass.")
                    else:
                        if _end_new:
                            _n = f" ({len(_end_new)} new failure(s) attributable to this diff)"
                        elif _end_new is not None and not _end_cur:
                            _n = " (no recognized diagnostics to attribute -- not treated as no-regression)"
                        elif _end_new is not None and not _end_did_work:
                            _n = " (0 new failures but no tree change -- not treated as no-regression)"
                        else:
                            _n = ""
                        log(f"[worker] VERIFY FAILED (exit {result.returncode}){_n}. "
                            f"Do not trust this output as-is.")
    else:
        log("[worker] No --verify command given. Output has NOT been verified -- "
            "build/test it before trusting it.")

    # Accept-on-verify-pass despite no task_complete (2026-08-30, the owner: "we're still
    # choking these processes"). Root cause of a class of false NON-CONVERGENCE:
    # qwen3-coder reliably WRITES a correct deliverable and verifies it by running it
    # via run_bash, but often never emits the task_complete tool call to SIGNAL done --
    # so it edits/re-runs to the iteration cap and the run is scored NO CONVERGENCE
    # even though the objective --verify passes against real, changed work. That's a
    # model-signalling gap, not incomplete work. So: if the run did NOT converge (no
    # task_complete) and was NOT paused, but a --verify was given and PASSED (the
    # vacuous-pass / baseline-delta guards above already applied, so verify_passed here
    # means a genuine pass against a genuinely-changed tree), treat it as converged.
    # Narrowly scoped: only fires when there's a real verify that really passed -- a run
    # with no --verify, or a failing verify, still reports non-convergence as before.
    if (verify and verify_passed and not converged and not paused_for_review
            and _dispatch_metrics.get("early_abort") != _wr.REASON_STOPGATE):
        log("[worker] ACCEPTING as converged despite no task_complete: hit the iteration "
            "cap without the model signalling done, but the --verify PASSED against real "
            "changed work. Known qwen3-coder gap (verifies by running, doesn't emit "
            "task_complete). Scored as success, not a false non-convergence.")
        converged = True

    # Always logged, per standing instruction -- not conditional on
    # success, since a failed/non-converged dispatch is exactly the data
    # worth having a record of too.
    log_dispatch_to_vault(model, task, converged, verify_passed, log_path,
                          baseline_no_regression=_baseline_no_regression,
                          dispatch_tag=dispatch_tag, host=host, task_kind=task_kind)

    # This log USED to be success-only by design (the owner's call): a crashed or
    # non-converged run's token counts aren't a clean signal for "how much
    # context does a task like this actually need", and would pollute the
    # threshold analysis the log exists for.
    #
    # That rationale still holds and is PRESERVED -- by the `status` field, not
    # by omission. Any context-threshold analysis filters status == "converged"
    # and sees exactly the same population it saw before; not one converged row
    # changes meaning. What omission also did, though, was make failures
    # unanswerable: asked on 2026-09-01 whether over-scoped jobs predictably
    # die at the iteration cap, the file could not answer, because all 309 rows
    # were status=converged and the outcome variable had a single value. That
    # is total survivorship bias -- the two cap-deaths that prompted the
    # question were simply absent. A log that records only successes cannot be
    # used to study failure, and studying failure is now the point.
    #
    # NOTE FOR CONSUMERS: dispatch-tally.py counts rows without filtering
    # status, so its dispatch count will now include failures. That is more
    # correct -- a failed dispatch still consumed a GPU slot -- but it is a
    # step change in a tracked number, so the tally prints the breakdown.
    _dispatch_metrics["verify_passed"] = verify_passed
    # Distinguish a clean pass from an accepted no-regression-on-broken-baseline run so
    # the metrics JSONL doesn't over-count clean verify passes (baseline-delta, 2026-08-30).
    _dispatch_metrics["baseline_no_regression"] = _baseline_no_regression
    # `converged` is checked FIRST and unqualified, which deliberately does NOT
    # mirror the return-code precedence below. A converged run that also paused,
    # or that failed verify, has always written status="converged" with the
    # detail in verify_passed -- reordering to match the exit code would
    # silently redefine all 309 existing rows. So status and exit code diverge
    # in exactly those cases (verified exhaustively: 4 of 12 reachable
    # combinations, all of them converged=True), and that divergence is the
    # existing contract, not a bug. Read `status` for what the run achieved and
    # the exit code for how it terminated; they answer different questions.
    # The non-converged branches below DO mirror the return-code precedence.
    _dispatch_metrics["status"] = (
        "converged" if converged
        else "paused" if paused_for_review
        else "done_unconverged" if (verify and verify_passed)
        else "verify_failed" if verify
        else "unconverged")
    # FIX 4 (2026-09-13): a DISTINCT terminal reason so a context/scaffold defect
    # is never logged (or read off the queue) as bare model incapacity. Only a
    # genuinely FAILED terminal outcome is classified -- a converged run has no
    # failure to explain, and a paused run already carries pause_reason_code.
    #   read_thrash / write_thrash : the anti-thrash early abort fired
    #   reasoning_freeze           : the model's thinking became a fixed point (2026-09-19)
    #   context_starved            : the run was starving the context window
    #   nonconvergence             : budget exhausted on genuinely varied work
    if converged or paused_for_review:
        terminal_reason = None
    else:
        _ea = _dispatch_metrics.get("early_abort")
        if _ea in ("write_thrash", "read_thrash", "reasoning_freeze", "output_cap_loop",
                   "prose_loop", "wall_budget") + _wr.NEW_EXIT_REASONS:
            terminal_reason = _ea
        elif _ea == "thrash_zero_diff":          # existing read-only thrash abort
            terminal_reason = "read_thrash"
        elif num_ctx and _dispatch_metrics.get("peak_prompt_tokens", 0) >= 0.9 * num_ctx:
            terminal_reason = "context_starved"
        else:
            terminal_reason = "nonconvergence"
        # The fixed lane ran first and FAILED, and the open loop that followed ended with nothing more
        # specific to say: report the lane's reason (queue tables classify fixed_lane_*).
        if terminal_reason == "nonconvergence" and _fixed_lane_reason:
            terminal_reason = _fixed_lane_reason
    _dispatch_metrics["terminal_reason"] = terminal_reason
    # SALVAGE: a run that stopped without converging (loop_detected & friends, stop_gate_failed, the
    # iteration cap) keeps its diff as a patch file next to the transcript, plus whether the end-of-run
    # verify passed -- a near-complete diff is evidence, not garbage. Best-effort, never raises.
    if terminal_reason and not paused_for_review:
        try:
            _salv = save_salvage_patch(cwd, Path(str(log_path)).with_suffix(".salvage.patch"))
            if _salv:
                _dispatch_metrics["salvage"] = dict(_salv, verify_passed=verify_passed,
                                                    terminal_reason=terminal_reason)
                log(f"[worker] SALVAGE: kept the non-converged diff ({_salv['files']} file(s), "
                    f"{_salv['bytes']} bytes, verify_passed={verify_passed}) at {_salv['path']} "
                    f"-- exit reason {terminal_reason}.")
        except Exception as _se:
            log(f"[worker] salvage error (ignored): {_se!r}")
    # EDIT CHECKPOINT report (EDIT_CHECKPOINT=1): the run ended WITHOUT convergence and the end-of-run
    # verify did not pass, but a mid-run verify DID pass with the required criteria met -- the model
    # reached a good patch and then lost it. Report the snapshot (never applied automatically).
    if (_edit_checkpoint_enabled() and not converged and not paused_for_review
            and verify_passed is not True and _EDIT_CKPT.get("info")):
        _ck = _EDIT_CKPT["info"]
        _dispatch_metrics["edit_checkpoint"] = {
            "path": _ck["path"], "bytes": _ck["bytes"], "files": _ck["files"],
            "passes": _ck.get("passes"), "exit": "unconverged_with_passing_checkpoint",
            "terminal_reason": terminal_reason, "end_verify_passed": verify_passed}
        log(f"[worker] EDIT-CHECKPOINT: run ended UNCONVERGED ({terminal_reason}) and the end-of-run "
            f"verify did not pass, but the verify PASSED mid-run (pass #{_ck.get('passes')}). The passing "
            f"snapshot is kept at {_ck['path']} ({_ck['files']} file(s)); restore with "
            f"`git apply {_ck['path']}` on a clean tree. EXIT NOTE: unconverged_with_passing_checkpoint.")
    if terminal_reason:
        # Greppable marker the queue daemon parses out of the job log (same
        # channel as RESUMABLE TRANSCRIPT), so the terminal state carries WHY.
        log(f"[worker] TERMINAL REASON: {terminal_reason}")
    _dispatch_metrics["wall_time_s"] = round((datetime.now(timezone.utc) - _dispatch_start).total_seconds(), 1)
    _usage_line = usage_finalize(_dispatch_metrics)
    if _usage_line:
        log(_usage_line)        # greppable: the queue dashboard parses this out of the job log
    _dispatch_metrics["transcript_path"] = str(log_path)
    _dispatch_metrics["max_iters"] = max_iters
    # Did it die ON the cap? The single most useful field for the question that
    # motivated this change -- p95 of converged runs sat exactly on the cap,
    # which is a censoring signature, and this makes it directly countable
    # instead of inferred from a histogram.
    _dispatch_metrics["hit_iter_cap"] = bool(
        isinstance(max_iters, int) and _dispatch_metrics.get("iterations", 0) >= max_iters)
    _dispatch_metrics["files_changed"] = _changed_file_count(cwd)
    _dispatch_metrics["web_search"] = dict(_WEB_SEARCH_CALLS)
    write_dispatch_metrics(_dispatch_metrics)

    if is_local_ollama and cleanup_after:
        evict_model(model)
    elif api_style == "ollama":
        # Leave it warm for 24h so the next dispatch of this model (very
        # likely, given pick_host() now routes consistently) skips the cold
        # load -- see set_keep_alive()'s docstring. Skipped when
        # cleanup_after already evicted the model outright.
        set_keep_alive(host, model, "24h")

    if live is not None:
        live.close()

    if paused_for_review:
        # Distinct from both 1 (verify failed) and 2 (genuinely ran out of budget): the queue
        # daemon uses this to mark the job "paused" instead of "failed" and relaunch it with
        # --resume <transcript>. Covers BOTH pause sources -- the model's own review gates AND
        # an external SIGTERM from the daemon's promote flow -- which are the same situation
        # for a caller: gracefully stopped, resumable, not a failure.
        return EXIT_CODE_PAUSED
    if verify and not verify_passed:
        return 1
    if not converged and verify and verify_passed:
        # Mirror image of the vacuous-pass guard above: the loop didn't exit tidily
        # (model kept iterating/rambling after finishing, or otherwise ran out of
        # budget), but --verify genuinely passed -- and if task_kind=="coding" the
        # vacuous-pass guard already required real tree changes or file writes to
        # leave verify_passed True at all (a vacuous pass gets overridden to False
        # before this point), so reaching here with verify_passed=True means real,
        # confirmed work exists. Report that distinctly instead of plain FAILED.
        log(f"[worker] DONE-BUT-UNCONVERGED: verify passed with real completed work, "
            f"but the loop didn't exit cleanly ({total_iters} iterations used) -- "
            f"reporting exit {EXIT_CODE_DONE_UNCONVERGED}, not plain DID-NOT-CONVERGE, "
            f"so this isn't discarded unread by anything triaging on status alone.")
        return EXIT_CODE_DONE_UNCONVERGED
    return 0 if converged else 2


def main():
    ap = argparse.ArgumentParser(description="Dispatch a coding task to a local Ollama model, bypassing opencode.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--direct-ok", action="store_true",
                     help="Run without going through ollama-queue.py. Off by default: a direct "
                          "invocation is invisible to the queue dashboard AND to the queue's "
                          "cross-host coordination, so two dispatches can silently collide on "
                          "one host. Use only for a deliberate one-off you are watching.")
    ap.add_argument("--host", default=None,
                     help="Ollama host to dispatch to. Omit to auto-pick based on which of the "
                          "CONFIGURED hosts (~/.ollama-dispatch/hosts.json, editable by hand or "
                          "from the dashboard's Settings panel) the model actually fits -- see "
                          "pick_host()/load_ollama_hosts(). Pass "
                          "explicitly to override (e.g. a third/ad-hoc host, or an OpenAI-style "
                          "--api openai endpoint, which pick_host doesn't know about).")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task", required=True,
                     help="Task description. Still required when --resume is given (keeps the "
                          "CLI consistent), but when resuming the actual message history comes "
                          "from the resumed transcript file, not reconstructed from --task.")
    ap.add_argument("--resume", default=None,
                     help="Path to a previously saved transcript JSON to resume from: its "
                          "messages are reloaded and the loop continues from where that run "
                          "left off (iteration numbering continues, and --max-iters means how "
                          "many MORE iterations are allowed this session). Model/host come "
                          "from the CLI args, so resuming with a DIFFERENT model than started "
                          "the task is a supported case, not an error. The transcript keeps "
                          "updating the same file across pause/resume cycles.")
    ap.add_argument("--verify-failed-at-baseline", action="store_true",
                    help=("the queue's pre-flight ran --verify in this cwd BEFORE the model "
                          "touched anything and it FAILED. That makes a still-failing verify "
                          "UNINFORMATIVE, not excusable: it means either the fix did not land "
                          "or the environment is broken, and neither is evidence of completion. "
                          "Suppresses the BASELINE-BROKEN auto-accept: the full verify output "
                          "is fed back as work to do while completion claims remain, and the "
                          "run pauses for review only once the claim cap is reached."))
    ap.add_argument("--scored-arm", action="store_true",
                    help=("this run is a SCORED BAKE-OFF ARM, staged from a verify PROVEN to "
                          "fail at baseline. Implies --verify-failed-at-baseline (a stronger "
                          "statement than the queue pre-flight's observation) AND disables "
                          "baseline-diagnostic subtraction, because for a scored arm the "
                          "baseline failures ARE the task -- subtracting them would hide the "
                          "only diagnostics that matter and let an untouched bug read as 'no "
                          "new failures'. Set by bakeoff-fire.py; recorded in dispatch-metrics "
                          "as scored_arm so a scorer never pools scored and unscored runs."))
    ap.add_argument("--verify", default=None, help=(
        "Shell command to run after the loop completes, e.g. 'npm run build'. Two authoring "
        "rules, confirmed real-incident 2026-08-29 (Fable review): (1) a negative-grep guard "
        "('! grep pattern') cannot distinguish a forbidden command being EXECUTED from that "
        "same text merely being echoed/printed/commented -- exclude echo/printf/comment lines "
        "or it will fail correct work that only prints the pattern. (2) verify should assert "
        "something that was FALSE before the dispatch ran, not just something trivially true of "
        "any file (a bare syntax check like 'bash -n script.sh' passes on an UNMODIFIED file too "
        "-- the harness now catches the zero-files-touched case, see the vacuous-pass guard in "
        "run_task, but a check that's simply too weak to fail on wrong-but-syntactically-valid "
        "work is not caught by anything)."
    ))
    ap.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS)
    ap.add_argument("--temperature", type=float, default=None,
                     help="Override the model profile's temperature (default: model_profiles.yaml for --role)")
    ap.add_argument("--role", choices=sorted(_mp.load_profiles()["roles"]), default="author",
                     help="Which model-card mode to use: author/coding = thinking_coding, gate/review = review (non-thinking). See model_profiles.yaml")
    ap.add_argument("--num-ctx", type=int, default=None,
                     help="Override the profile ctx (default: model_profiles.yaml)")
    ap.add_argument("--num-ctx-bumps", type=int, default=0,
                    help="how many times the queue watchdog raised this job's context "
                         "before this run; recorded as a metric, never a penalty")
    ap.add_argument("--top-p", type=float, default=None,
                     help="Nucleus sampling cutoff. Added 2026-08-22: both Qwen's and DeepSeek-R1's "
                          "own model cards specifically warn against temperature=0 (greedy decoding) "
                          "-- documented to cause endless-repetition failures, confirmed live as the "
                          "root cause of a real runaway-generation crash tonight. Their recommended "
                          "settings pair temperature=0.6 with top_p=0.95. Omit to leave Ollama's "
                          "default (unset here).")
    ap.add_argument("--top-k", type=int, default=None,
                     help="Top-k sampling cutoff. Qwen's recommended setting is 20, paired with the "
                          "temperature/top_p above -- see --top-p's help for why this exists.")
    ap.add_argument("--searxng-host", default=DEFAULT_SEARXNG_HOST,
                     help="Self-hosted SearXNG instance for web_search (see deploy notes in the vault -- "
                          "not yet deployed as of 2026-08-21, web_search will error until it is).")
    ap.add_argument("--system-prompt-file", default=None,
                     help="Path to a file with a custom system prompt, replacing the default. "
                          "Needed for devstral, which produces zero tool calls on this Ollama build "
                          "without its own OpenHands-scaffold system prompt.")
    ap.add_argument("--task-kind", choices=["coding", "research"], default="coding",
                     help="What counts as 'real progress' for the corrective-nudge safeguard. "
                          "\"coding\" (default) requires an actual write_file/edit_file call before "
                          "a no-tool-call response is accepted without a nudge -- catches a model "
                          "narrating a change instead of saving it. \"research\" accepts ANY tool "
                          "call (e.g. web_fetch) as real progress, since a research task's "
                          "deliverable is the final text answer, not a file -- added 2026-08-28 "
                          "after confirming live that the coding-only definition guaranteed a "
                          "false-positive nudge on every research dispatch, which pushed at least "
                          "one model into writing its unverified answer to a file it was never "
                          "asked to produce.")
    ap.add_argument("--facts-provided", action="store_true",
                     help="Suppresses the research task-kind's anti-fabrication nudge entirely. "
                          "Use for a two-pass 'write the final answer' dispatch where the facts "
                          "were already gathered and verified in a prior pass and are handed in "
                          "directly in the task text -- without this flag the nudge fires because "
                          "it can only see zero web_fetch calls THIS session, and cannot tell that "
                          "apart from a model that never did any real research at all. Added "
                          "2026-08-28 after the flag's absence forced a model with genuinely real, "
                          "pre-verified facts to rewrite a correct answer into a false 'could not "
                          "confirm anything, all fetches failed' disclaimer.")
    ap.add_argument("--read-file-max-chars", type=int, default=None,
                     help="Override the per-read page cap (default: READ_FILE_MAX_CHARS, "
                          "additionally clamped to ~1/8 of --num-ctx).")
    ap.add_argument("--web-fetch-max-chars", type=int, default=None,
                     help="Override the per-fetch truncation limit (default: WEB_FETCH_MAX_CHARS, "
                          "currently 5000 chars). The default is tuned for open-ended multi-fetch "
                          "collection (avoids accumulated-context OOM across several fetches in one "
                          "pass) but truncates a single real multi-page document before content that "
                          "matters -- confirmed live 2026-08-28: a real NV Energy rate PDF's page-1 "
                          "boilerplate alone consumed the whole 5000-char budget, so the page-2 table "
                          "with the actual answer never reached the model, which correctly said 'not "
                          "specified' rather than fabricate. Raise this for a dispatch doing few, "
                          "targeted fetches of known-large documents; leave it alone for open-ended "
                          "multi-fetch research where the original OOM risk still applies.")
    ap.add_argument("--min-web-fetches", type=int, default=0,
                     help="Research task_kind only: require at least this many SUCCESSFUL "
                          "web_fetch calls before the task is allowed to converge -- a hard "
                          "requirement, not just a nudge. Added 2026-08-28 after confirming live "
                          "(llama3.1:8b, EV-charging-network discovery, 4 consecutive attempts) "
                          "that a model can repeatedly find the right pages via web_search and "
                          "just never fetch them, converging on unverified snippet claims with "
                          "iteration budget to spare -- the existing one-time fabrication nudge "
                          "only asks the model to hedge, it doesn't force real verification, and "
                          "in that same session the model responded to the nudge by falsely "
                          "claiming a fact was 'verified through web_fetch' when zero fetches had "
                          "ever succeeded. Default 0 (off) preserves prior behavior for tasks "
                          "where search-only answers are acceptable.")
    ap.add_argument("--cleanup-after", action="store_true",
                     help="Evict this model from the local disk cache after the task completes "
                          "(only blobs not shared by another cached model are removed). Trades "
                          "disk space for a repeated copy-from-SMB cost on the next dispatch of "
                          "this model -- omit to keep it cached (default, recommended when disk "
                          "space isn't tight).")
    ap.add_argument("--manual-tools", action="store_true",
                     help="Bypass Ollama's native tool_calls parsing entirely: inject the tool "
                          "schemas as plain text in the system prompt and parse the model's "
                          "response for a {\"name\":...,\"arguments\":...} object ourselves. "
                          "Needed for deepseek-r1 distills (confirmed 2026-08-21: their Ollama "
                          "template never renders the native `tools` field into the prompt at "
                          "all, and native tool_calls stays null even on the community "
                          "'MFDoom/deepseek-r1-tool-calling' build -- see Ollama-Dispatch-Log.md "
                          "and github.com/ollama/ollama/issues/8517). Confirmed working live "
                          "against deepseek-r1:14b across write_file/read_file tasks including "
                          "multi-turn continuation and correct termination.")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama",
                     help="Which API shape --host speaks. 'ollama' (default) targets Ollama's "
                          "native /api/chat and /api/tags|pull for model management. 'openai' "
                          "targets an OpenAI-compatible /v1/chat/completions endpoint (llama-server, "
                          "etc.) instead -- no model pull/discovery is attempted (the server already "
                          "has exactly one model loaded via its own -m flag), just a /health check. "
                          "Added 2026-08-22 to route around a confirmed upstream Ollama bug "
                          "('no user query found in messages', github.com/ollama/ollama/issues/17778) "
                          "that crashes qwen3.8/some other models even on trivial requests -- "
                          "llama-server renders the GGUF's own embedded chat template directly and "
                          "doesn't run Ollama's custom Go renderer code, so it doesn't hit this bug.")
    ap.add_argument("--api-key-file", default=None,
                     help="--api openai only: a file holding the Bearer key for a key-gated "
                          "OpenAI-style endpoint that is NOT Darkbloom (Darkbloom's key is read "
                          "from ~/.darkbloom/local.json automatically). Read at call time and sent "
                          "only to --host; the key itself never appears in argv or the log. Used by "
                          "bake-off arms (e.g. Strata over an ssh tunnel); the queue never passes it.")
    ap.add_argument("--plan", default=None,
                     help="Queue-supplied resolved-lane PLAN file (lane_plan.py; sha-verified with --plan-sha). "
                          "SHADOW by default: the legacy path still decides and any difference is logged to "
                          "~/.ollama-dispatch/conformance/plan-mismatch.jsonl; LANE_PLAN_ENFORCE=1 makes the plan "
                          "authoritative. Ignored when WORKER_PLAN=0 or LANE_PLAN=0.")
    ap.add_argument("--plan-sha", default=None, help="Expected sha of --plan (the queue's value).")
    ap.add_argument("--key-fd", type=int, default=None,
                     help="Inherited pipe fd holding the Darkbloom key (queue pass_fds; never argv/env/disk). "
                          "Read once at start.")
    ap.add_argument("--chat-timeout", type=int, default=CHAT_TIMEOUT_S,
                     help=f"Per-request timeout (seconds) for the main dispatch-loop chat call "
                          f"(default {CHAT_TIMEOUT_S}s). Does NOT affect model warmup ({WARMUP_TIMEOUT_S}s, "
                          f"already generous and separate) -- only the per-iteration call. Raise this "
                          f"for a --resume retry after a run crashed on repeated timeouts at exactly "
                          f"the default value with a large accumulated context (confirmed real "
                          f"2026-08-28: a 27B model given ~35-40K tokens of context genuinely needed "
                          f"longer than 1200s to respond, not stuck/hung -- the backend was still "
                          f"actively computing the whole time).")
    ap.add_argument("--max-tokens", type=int, default=None,
                     help="Hard cap on generated tokens per chat call (default: model profile max_tokens, >=32768). "
                          f"Added 2026-08-28 after a real runaway-generation incident (an Unraid "
                          f"dispatch generated past 123,000 tokens with no stop token, confirmed via "
                          f"the backend's own print_timing log, not guessed). Ollama native: "
                          f"options.num_predict. --api openai: top-level max_tokens.")
    ap.add_argument("--capture-final-as", default=None,
                     help="Filename (relative to cwd) of the run's expected deliverable. If the run "
                          "ends with that file NOT written but a final text answer present, the worker "
                          "writes the final text to it and logs capture=fallback -- for models that put "
                          "a review/answer in their text reply instead of writing the file (Fable "
                          "2026-08-30, nemotron-cascade-2). Runs before verify; skipped on a paused run.")
    ap.add_argument("--preserve-reasoning", action="store_true",
                     help="--api openai only: keep a thinking model's `reasoning_content` instead of "
                          "dropping it. Copies it to the message's `thinking` field, and -- only when "
                          "the turn produced NO content and NO tool call -- uses it as `content` so an "
                          "otherwise-empty turn is not lost. Needed by Bonsai (Ternary-Bonsai-2-27B on "
                          "llama-server), which returns its output in reasoning_content; ollama-queue.py "
                          "passes this automatically for jobs routed to the bonsai lane. Opt-in so the "
                          "existing qwen3.8 llama-server bypass is unchanged.")
    ap.add_argument("--repeat-penalty", type=float, default=None,
                     help="Sampler repeat penalty (default: model profile repetition_penalty; 1.0 = disabled). "
                          f"Same incident as --max-tokens: temperature=0 (fully greedy, the common "
                          f"case here) with repeat_penalty=1.0 is a documented llama.cpp infinite-"
                          f"repetition-loop combination (github.com/ollama/ollama/issues/3759, "
                          f"ggml-org/llama.cpp discussion #3005) -- this file never set it before, so "
                          f"every past dispatch ran at the disabled default.")
    ap.add_argument("--think", choices=["auto", "on", "off"], default="auto",
                     help="Native Ollama reasoning-mode toggle (top-level `think` key). auto (default) "
                          "= omit it, model decides / no behaviour change. off = disable reasoning -- "
                          "use for tool-driven dispatches on HYBRID models (qwen3.5:9b etc.), where "
                          "thinking-on burns the whole token budget on the reasoning field and returns "
                          "empty content / no tool call (e2 measured this). on = force it. Always-"
                          "thinking models (nemotron-a3b) reject an explicit value with an HTTP error; "
                          "there is no auto-retry -- use auto for those models. auto now means "
                          "\"the model profile's enable_thinking for --role\" (model_profiles.yaml); "
                          "on --api openai it is sent as chat_template_kwargs.enable_thinking.")
    ap.add_argument("--live-log", default=None,
                     help="Opt-in live streaming log: when set, the default native-Ollama-tools "
                          "path streams each model response and appends tagged, colored status "
                          "lines (thinking checkpoints, tool calls/results, iteration "
                          "boundaries, a final result box) to this file for `tail -f` viewing. "
                          "This is IN ADDITION to the normal log() output, not a replacement. "
                          "When omitted, behavior is exactly as before (blocking call_ollama, "
                          "no streaming). Only the default native-tools Ollama path streams; "
                          "--manual-tools / --api openai runs still use the blocking call even "
                          "with this flag set.")
    ap.add_argument("--dispatch-tag", default=None,
                     help="Short tag prefixed to every --live-log line ([tag] ...) so one "
                          "dispatch can be followed among several concurrent dispatches "
                          "sharing the same log file. Defaults to the --cwd basename.")
    ap.add_argument("--claude-prep-tokens", type=int, default=None,
                     help="Claude's own output-token cost of getting to this dispatch (investigation, "
                          "writing the task spec) -- a separate figure from anything measured here, "
                          "logged alongside it in dispatch-metrics.jsonl so the two are directly "
                          "comparable. Compute via claude-token-cursor.py: run it once before starting "
                          "prep, once again right before this dispatch, pass the delta here. Omit if "
                          "not tracking this for a given dispatch.")
    ap.add_argument("--extra-iterations", type=int, default=0,
                     help="Raise this process's iteration ceiling by N (capped at %d) for ONE round: the "
                          "continuation allowance for a job the queue marked nonconvergence_progressing "
                          "(continuation_allowance.extra_iterations). Additive to the ceiling the "
                          "--resume/--max-iters rules produce; 0 = unchanged." % EXTRA_ITERATIONS_MAX)
    ap.add_argument("--mode", choices=["auto", "open", "fixed"], default="open",
                     help="open (default): the tool loop only. fixed / auto: first try the one-shot "
                          "repair lane (fixed_lane.py) on small, localized, fast-verify tasks; on a pass "
                          "finish via the normal verify/gate path, otherwise continue in the open loop "
                          "seeded with the lane's best diff + last verify output. Ineligible tasks, "
                          "resumes, research tasks and non-OpenAI lanes always run the open loop.")
    ap.add_argument("--fixed-target", action="append", default=None,
                     help="Target file for the fixed lane (repeatable). Default: the files the TASK "
                          "declares (Entry point / Required files).")
    ap.add_argument("--budget-policy", default=None, metavar="NAME[,NAME...]",
                     help="Switchable agent-budget policies (worker_budget.py): progress_extend, "
                          "budget_visible, read_window, mask_stale_reads, prefetch_excerpt, "
                          "checkpoint_revert, read_streak_nudge, or `all` / `none`. Default (flag "
                          "absent): a `Budget-policy:` line in the task, else env WORKER_BUDGET_POLICY, "
                          "else none = unchanged behaviour. Coding runs only. See budget-policy-ab.md.")
    ap.add_argument("--prefetch-excerpt", action="append", default=None, metavar="PATH[:A-B]",
                     help="With the prefetch_excerpt policy: inject this file's head+outline (or lines "
                          "A-B) into the first prompt (repeatable). Also read from `Prefetch:` lines in "
                          "the task / TASK.md / AUTO-TASK.md.")
    args = ap.parse_args()
    if args.budget_policy is not None and _bb is not None:
        _bp_chk, _bp_bad = _bb.parse_policy_list(args.budget_policy)
        if _bp_bad:
            ap.error(f"--budget-policy: unknown name(s) {_bp_bad}; known: {', '.join(_bb.POLICIES)}, all, none")

    # Dispatch must go through ollama-queue.py. Enforced here rather than left
    # as a rule because the rule has failed repeatedly: a direct `nohup
    # ollama-worker.py ...` run does real work but never appears on the queue
    # dashboard and is invisible to the queue's own host-coordination, which is
    # exactly the collision the queue exists to prevent (2026-08-29: a direct
    # run occupied Studio while the queue believed Studio was free). The queue
    # stamps OLLAMA_DISPATCH_VIA_QUEUE on every worker it launches.
    if not os.environ.get("OLLAMA_DISPATCH_VIA_QUEUE") and not args.direct_ok:
        sys.stderr.write(
            "refusing to run: this worker was not launched by ollama-queue.py.\n"
            "Direct runs are invisible to the queue dashboard and to its cross-host\n"
            "coordination, so they can collide with queued work on the same host.\n\n"
            "Enqueue it instead:\n"
            "  python3 ~/bin/ollama-queue.py enqueue --model MODEL --host auto \\\n"
            "      --cwd DIR --task-file FILE --label NAME\n\n"
            "Pass --direct-ok only for a deliberate one-off you are actively watching.\n"
        )
        # Exit 4, not 2: the worker already uses 2 for DID-NOT-CONVERGE, and
        # conflating "refused to start" with "ran but gave up" sent one real
        # diagnosis down the wrong path entirely.
        sys.exit(4)

    if _pr is not None:     # hardening Wave 2: load the resolved-lane plan + the key fd (shadow: log-only; never raises)
        _pr.init_worker(args.plan, args.plan_sha, args.key_fd, job=os.environ.get("OLLAMA_DISPATCH_VIA_QUEUE"))
    host = args.host
    if args.api_key_file:
        if args.api != "openai" or not host:
            sys.stderr.write("--api-key-file needs --api openai and an explicit --host\n")
            sys.exit(4)
        if not os.path.isfile(args.api_key_file):
            sys.stderr.write(f"--api-key-file {args.api_key_file}: no such file\n")
            sys.exit(4)
        set_openai_key_file(args.api_key_file, host)
    if host is None:
        if args.api == "openai":
            # pick_host only knows about the two Ollama-native hosts; an
            # OpenAI-style endpoint (llama-server, etc.) has no sensible
            # auto-pick. Default to the dedicated llama-server
            # (start-llama-server-qwen3.8.sh, fixed PORT=8091, started with
            # --jinja so tool-calling works) instead of erroring out. We do
            # NOT launch that script here -- if nothing is listening,
            # ensure_model_ready() says so plainly. An explicit --host
            # always overrides this default, unchanged from before.
            host = DEFAULT_OPENAI_HOST
            log(f"[worker] --api openai without --host: defaulting to {DEFAULT_OPENAI_HOST} "
                f"(Darkbloom's local endpoint; it must already be running -- "
                f"this script does not start it)")
        else:
            host = pick_host(args.model)

    # CRASH PATH. write_dispatch_metrics' docstring has always claimed it is
    # "called both from run_task's normal completion path and from main()'s
    # crash handler, so a context-exhaustion crash -- the exact failure mode
    # this log exists to eventually let the owner threshold against -- still gets a
    # real entry instead of silently vanishing." There was no crash handler.
    # The claim was aspirational, and the crashes it names were exactly the
    # rows missing from the file. Now it is true.
    #
    # Catches Exception only: SystemExit and KeyboardInterrupt propagate
    # untouched, and so does the daemon's SIGTERM promote flow, which is a
    # graceful pause and already has its own path. Re-raises after logging --
    # this observes the failure, it must never swallow it.
    try:
        rc = run_task(
            args.model, host, args.cwd, args.task, args.verify,
            args.max_iters, args.temperature, args.num_ctx, args.searxng_host,
            args.system_prompt_file, args.cleanup_after, args.manual_tools,
            top_p=args.top_p, top_k=args.top_k, api_style=args.api,
            claude_prep_tokens=args.claude_prep_tokens,
            resume_from=args.resume,
            task_kind=args.task_kind,
            chat_timeout=args.chat_timeout,
            max_tokens=args.max_tokens,
            repeat_penalty=args.repeat_penalty,
            facts_provided=args.facts_provided,
            web_fetch_max_chars=args.web_fetch_max_chars,
            read_file_max_chars=args.read_file_max_chars,
            verify_failed_at_baseline=args.verify_failed_at_baseline,
            scored_arm=args.scored_arm,
            num_ctx_bumps=args.num_ctx_bumps,
            min_web_fetches=args.min_web_fetches,
            capture_final_as=args.capture_final_as,
            preserve_reasoning=args.preserve_reasoning,
            role=args.role,
            live_log=args.live_log,
            dispatch_tag=args.dispatch_tag or (Path(args.cwd).name or "dispatch"),
            think={"on": True, "off": False, "auto": None}[args.think],
            extra_iterations=args.extra_iterations,
            mode=args.mode,
            fixed_targets=args.fixed_target,
            budget_policy=args.budget_policy,
            prefetch_specs=args.prefetch_excerpt,
        )
    except Exception as e:
        if _dispatch_metrics.get("status") == "running":
            _dispatch_metrics["status"] = "crashed"
            _dispatch_metrics["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            _dispatch_metrics["web_search"] = dict(_WEB_SEARCH_CALLS)
            # FIX 4: a crash is most often context exhaustion -- classify it so
            # the queue does not read it as bare model incapacity either.
            try:
                _nc = args.num_ctx or 0
                _peak = _dispatch_metrics.get("peak_prompt_tokens", 0)
                _tr = "context_starved" if (_nc and _peak >= 0.9 * _nc) else "crashed"
                _dispatch_metrics["terminal_reason"] = _tr
                log(f"[worker] TERMINAL REASON: {_tr}")
            except Exception:
                pass
            write_dispatch_metrics(_dispatch_metrics)
        raise
    sys.exit(rc)


if __name__ == "__main__":
    main()
