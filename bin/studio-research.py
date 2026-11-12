#!/usr/bin/env python3
"""studio-research.py -- a multi-stage deep-research orchestrator for local models.

WHY THIS EXISTS, AND WHY IT IS NOT ollama-worker.py --task-kind research
-----------------------------------------------------------------------
ollama-worker.py runs a single ReAct loop: one model decides, per turn, whether
to search, fetch, or answer. That shape has a ceiling that is not about model
size, and the vault has the receipts for it:

  * llama3.1:8b, 5 consecutive runs, found the right official pages via
    web_search and then simply chose not to web_fetch them, answering from
    300-char snippets.
  * Told "0/3 fetches done" by --min-web-fetches, the same model replied
    "I have successfully fetched 3 real page(s) so far." It could not be made
    honest by being asked to be honest.

Both failures share one root cause: the model is trusted to *perform and report*
its own evidence gathering. So this harness removes that trust entirely.

  The MODEL never fetches. The HARNESS fetches, deterministically, and hands
  the model text it did not choose. "I fetched 3 sources" stops being a claim
  a model can make, truthfully or otherwise -- it becomes a row count.

On top of that, every quote a model emits is checked as a literal substring of
the page the harness actually retrieved (verify_quote below). A fabricated quote
is dropped before it can reach synthesis. Fabrication is not discouraged here;
it is structurally unable to survive to the report.

The second thing this buys is that TOOL-CALLING ABILITY STOPS BEING A GATE. In a
ReAct loop a model that cannot emit clean tool-call JSON is unusable no matter
how well it reasons -- which is what disqualified deepseek-r1 and moresearch/swe7b
for the research role. Here, only text in and JSON out is ever required, so
strong reasoners with broken tool-call formatting become eligible again.

PIPELINE
--------
  1 PLAN      model  -> sub-questions + seed queries
  2 GATHER    harness-> search (multi-engine) -> dedupe -> parallel fetch -> evidence store
  3 EXTRACT   model  -> per source: verbatim quotes bearing on each sub-question
                        (harness drops any quote not literally present)
  4 GAP       model  -> which sub-questions are still thin -> new queries -> back to 2
  5 SYNTH     model  -> report written ONLY from surviving findings, cited [E#]
  6 VERIFY    model  -> per cited sentence, adversarially: does the quote support it?

Stages 1/4/5/6 are separate calls on purpose. A model asked in one breath to
gather, decide sufficiency, and write, grades its own homework at every step;
split apart, the gap check reads a finding list rather than its own memory of
having looked, and the verifier sees a sentence next to its evidence with no
recollection of having written it.
"""
from __future__ import annotations

import argparse
import atexit
import concurrent.futures as cf
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import darkbloom_chat as _dbk  # DARKBLOOM: OpenAI-protocol adapter for the local Darkbloom lane
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse the worker's fetcher rather than writing a second one. It carries fixes
# that were each paid for by a real failed dispatch -- PDF extraction via pypdf
# (raw PDF bytes fed back into /api/chat returned a hard 500 and killed a run),
# the AEM .model.json fallback for JS-rendered pages, and a browser UA. A fresh
# implementation here would reintroduce all three.
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "ollama_worker", str(Path(__file__).resolve().parent / "ollama-worker.py"))
    _worker = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_worker)
    WORKER_FETCH = _worker.tool_web_fetch
except Exception as _e:  # pragma: no cover - fallback keeps this runnable standalone
    WORKER_FETCH = None
    _WORKER_IMPORT_ERR = _e

DEFAULT_SEARXNG = "http://203.0.113.154:8080"
SEARCH_PACE_S = 2.0   # between successive searches; see stage_gather
DOMAIN_CAP = 3        # max sources per domain per round; see stage_gather
DEFAULT_HOST = _dbk.default_host()  # DARKBLOOM: local lane; F1/W2: sticky through a restart, the retired Ollama ONLY when Darkbloom is not configured

# ---- web-search accounting -------------------------------------------------
# Every searxng_search() call is one search request; count them so the
# Web-Search-Usage dashboard can see research-agent usage. ollama-worker.py
# writes dispatch-metrics.jsonl for its runs; the research agent never went
# through it, so its SearXNG traffic was invisible. Emit our own sibling file
# (ollama-queue-api.py sums both). Written once, at process exit, via atexit so
# every return path in main() -- including early aborts -- is covered.
SEARXNG_CALLS = 0
TAVILY_CALLS = 0
_RESEARCH_META: dict = {}
RESEARCH_METRICS = Path.home() / "bin" / "ollama-worker-logs" / "research-metrics.jsonl"


def _emit_research_metrics() -> None:
    if not _RESEARCH_META:
        return  # never got far enough to start a run (e.g. --help)
    try:
        RESEARCH_METRICS.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "studio-research",
            "model": _RESEARCH_META.get("model"),
            "question_preview": (_RESEARCH_META.get("question") or "")[:160],
            "searxng_calls": SEARXNG_CALLS,
            "tavily_calls": TAVILY_CALLS,
            # SearXNG aggregates the underlying engines; the dashboard's SearXNG
            # bucket is the right home for every research search request.
            "web_search": {"ollama": 0, "tavily": TAVILY_CALLS, "brave": 0, "searxng": SEARXNG_CALLS},
        }
        with open(RESEARCH_METRICS, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # metrics must never break a research run
# The 6 general-web engines ollama-worker.py settled on, plus the academic ones.
# Probed live 2026-08-30: brave and google cse both returning 20 results, bing
# returning results, duckduckgo and startpage CAPTCHA'd/suspended. Naming all of
# them means a total outage needs 6 independent engines down at once; naming the
# academic set separately means a paper-shaped question is not left to the
# general engines' idea of relevance.
GENERAL_ENGINES = "brave,google cse,bing,duckduckgo,startpage,duckduckgo web"
ACADEMIC_ENGINES = "arxiv,pubmed,semantic scholar,google scholar,openairepublications"

# Domains that reliably cost a fetch and return nothing a research report can
# cite -- image boards, video pages, and link aggregators whose text is titles.
JUNK_HOSTS = re.compile(
    r"(^|\.)(pinterest\.|instagram\.|facebook\.|tiktok\.|youtube\.|youtu\.be|"
    r"twitter\.|x\.com|flickr\.|imgur\.|unsplash\.|pexels\.|"
    # Below: measured, not assumed. Each of these was observed failing every
    # time it was drawn during this session's runs -- reddit and quora return
    # a shell with no extractable content, techpowerup and technical.city serve
    # 145-character JS stubs, and the retail sites either 403 or time out.
    # Spending a fetch slot on them costs a real source from the same wave.
    r"reddit\.|quora\.|techpowerup\.|technical\.city|bestbuy\.|"
    r"merriam-webster\.|dictionary\.com|eslteacher\.)", re.I)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[research {now()}] {msg}", flush=True)


# ---------------------------------------------------------------- model calls

class Model:
    """One Ollama chat endpoint. Every stage goes through here so that token
    accounting, timeouts and JSON-schema enforcement are identical everywhere."""

    def __init__(self, host: str, model: str, num_ctx: int, temperature: float,
                 timeout: int = 900, think: bool = False):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout = timeout
        self.think = think
        self.think_supported = True
        self.calls = 0
        self.total_s = 0.0
        self.truncated = 0

    def chat(self, system: str, user: str, schema: dict | None = None,
             temperature: float | None = None, num_predict: int = 2048) -> str:
        # DARKBLOOM: the local lane speaks OpenAI /v1, not Ollama /api/chat.
        if _dbk.is_darkbloom(self.host):
            _t0 = time.time()
            content, _fin = _dbk.chat(self.host, self.model, system, user, schema=schema,
                                      # None => the review profile from the model card
                                      # (model_profiles.yaml), not the hardcoded 0.3 / num_predict.
                                      temperature=None, max_tokens=None,
                                      think=self.think, timeout=self.timeout, role="review")
            self.calls += 1
            self.total_s += time.time() - _t0
            if _fin == "length":
                self.truncated += 1
                log(f"  [model] hit the {num_predict}-token cap -- output truncated")
            return content
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
                # Unbounded generation under a grammar constraint is a real hang:
                # nothing in a JSON schema forces a model to close an array, so a
                # confused model will happily emit list items until the context
                # ends. Cap it and report truncation rather than wait.
                "num_predict": num_predict,
            },
            # Ollama's structured-output mode. Local models are markedly worse at
            # "reply with only JSON" than at filling a schema, and every stage here
            # except synthesis needs machine-readable output. Constraining decoding
            # removes a whole class of parse failures rather than retrying them.
            "keep_alive": "30m",
        }
        # Thinking OFF by default, and this is not a style preference -- it is the
        # difference between working and not. Measured on qwen3.5:9b, this exact
        # planning call: with thinking on, all 900 permitted tokens went into the
        # `thinking` field, `content` came back EMPTY, and the call took 58s and
        # returned nothing parseable. With think=false: 274 tokens, clean JSON,
        # 4.8s. The vault records the sibling of this bug on qwen3:14b as
        # "thinking-mode swallows tool calls"; it swallows structured output the
        # same way, and for the same reason -- the reasoning trace and the answer
        # compete for one budget.
        if self.think_supported:
            body["think"] = self.think
        if schema is not None:
            body["format"] = schema
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # Models without a toggleable reasoning mode reject the parameter
            # outright. Drop it once and carry on rather than failing the run.
            if self.think_supported and "think" in detail.lower():
                log(f"  [model] {self.model} rejects the think parameter "
                    f"-- dropping it for the rest of this run")
                self.think_supported = False
                return self.chat(system, user, schema, temperature, num_predict)
            raise RuntimeError(f"ollama HTTP {e.code}: {detail}") from None
        dt = time.time() - t0
        self.calls += 1
        self.total_s += dt
        if data.get("done_reason") == "length":
            self.truncated += 1
            log(f"  [model] output hit the {num_predict}-token cap "
                f"(truncated; result may be unusable)")
        msg = data.get("message", {})
        content = msg.get("content", "")
        if not content and msg.get("thinking"):
            # Reasoning consumed the budget and never reached an answer. Say so
            # plainly: silently returning "" makes this look like a parse bug.
            log(f"  [model] EMPTY content but {len(msg['thinking'])} chars of "
                f"thinking -- reasoning consumed the whole budget")
        return content

    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float | None = None,
                  num_predict: int = 2048) -> dict | None:
        raw = self.chat(system, user, schema=schema, temperature=temperature,
                        num_predict=num_predict)
        try:
            return json.loads(raw)
        except Exception:
            # Constrained decoding makes this rare, but a reasoning model can still
            # emit a <think> block ahead of the object. Salvage the outermost {...}
            # rather than throwing the whole (expensive) call away.
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return None


# -------------------------------------------------------------------- search

def searxng_search(searxng: str, query: str, engines: str, want: int,
                   retries: int = 2) -> tuple[list[dict], list]:
    """Return (results, unresponsive_engines). Never raises."""
    global SEARXNG_CALLS
    SEARXNG_CALLS += 1
    url = f"{searxng.rstrip('/')}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "engines": engines})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "curl/8"})
    last_un: list = []
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log(f"  search ERROR {query!r}: {e}")
            return [], [["<transport>", str(e)[:80]]]
        results = data.get("results") or []
        last_un = data.get("unresponsive_engines") or []
        if results:
            return results[:want], last_un
        if not last_un:
            return [], []          # genuine zero-hit query; retrying cannot help
        if attempt < retries:
            time.sleep(8)          # a suspension near expiry often clears in seconds
    return [], last_un


# ------------------------------------------------------------- evidence store

class Evidence:
    def __init__(self, eid: str, url: str, title: str, query: str, engine: str):
        self.id = eid
        self.url = url
        self.title = title
        self.query = query
        self.engine = engine
        self.text = ""
        self.prefetched = ""   # full content supplied by the search API, if any
        self.chars = 0
        self.ok = False
        self.error = ""
        self.norm = ""     # whitespace-normalised text, for quote verification

    def to_dict(self) -> dict:
        return {"id": self.id, "url": self.url, "title": self.title,
                "query": self.query, "engine": self.engine, "chars": self.chars,
                "ok": self.ok, "error": self.error}


_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    return _WS.sub(" ", s or "").strip().lower()


def verify_quote(quote: str, ev: Evidence) -> bool:
    """Is this quote literally present in the page the harness retrieved?

    This is the load-bearing check of the whole design. A model that invents a
    plausible-sounding quote fails here and the finding is discarded, so no
    invented sentence can reach the report -- the report is assembled only from
    findings that survived.

    Three accepted forms, in decreasing strictness:

    1. Exact substring after whitespace normalisation.
    2. A 60-character verbatim prefix. Models routinely truncate the tail of a
       long quote; a 60-character run matching verbatim is not something a model
       produces by accident.
    3. An ELIDED quote -- "Launch Date ... Jan 2022" -- where every fragment
       between the ellipses is itself present in the page. Measured on
       OpenResearcher-30B: 4 of its 8 findings on a single page used this form,
       and all four were real facts correctly read off a spec table. Rejecting
       them threw away half the good evidence from the better model. Each
       fragment must still be genuinely present, so this admits sloppy
       transcription of real text without admitting invention.

    What it still rejects, correctly: a model writing its own sentence and
    calling it a quote. On the same run, "The TDP for the RTX 3080 12GB is 350
    watts, 30 watts higher than..." was rejected -- a fluent paraphrase of the
    page rather than anything the page says.
    """
    q = normalise(quote)
    if q in ev.norm and len(q) >= 25:
        return True
    if len(q) >= 60 and q[:60] in ev.norm:
        return True
    parts = [p.strip() for p in re.split(r"\.\.\.|\u2026", q) if p.strip()]
    if len(parts) > 1:
        solid = [p for p in parts if len(p) >= 4]
        # Every fragment must be real text from the page. The thresholds are
        # calibrated on actual output rather than picked round: an elided spec
        # -table quote like "Launch Date ... Jan 2022" is 11- and 8-character
        # fragments totalling 19, so a 12-character minimum per fragment
        # rejected exactly the evidence this branch exists to admit. Requiring
        # instead that ALL fragments be present, that the longest be >= 8, and
        # that >= 18 characters match in total, keeps a quote made of "the",
        # "a" and "of" from passing while letting a real table row through.
        if (solid and all(p in ev.norm for p in solid)
                and max(map(len, solid)) >= 8 and sum(map(len, solid)) >= 18):
            return True
    return False


def assign_subq(sid: str, statement: str, subqs: list[dict]) -> str | None:
    """Map a model-supplied sub-question id onto a real one.

    A verified quote is evidence; the id attached to it is a label. Dropping
    real evidence because the label was malformed is backwards, and it was
    happening constantly: on a live run qwen3.5:9b returned three correct,
    quote-verified findings from a Tom's Hardware page with the
    sub_question_id field left EMPTY on all three, so the harness discarded
    every one and reported "0 findings" from a page that answered the question
    outright. That single bug read as a model-capability problem for most of an
    afternoon.

    So: exact id, then case-insensitive, then fall back to whichever
    sub-question shares the most distinctive vocabulary with the statement.
    """
    ids = {q["id"]: q for q in subqs}
    if sid in ids:
        return sid
    up = (sid or "").strip().upper()
    for qid in ids:
        if qid.upper() == up:
            return qid
    words = set(re.findall(r"[a-z0-9]{4,}", (statement or "").lower()))
    if not words:
        return None
    best, score = None, 0
    for q in subqs:
        qw = set(re.findall(r"[a-z0-9]{4,}", q["question"].lower()))
        overlap = len(words & qw)
        if overlap > score:
            best, score = q["id"], overlap
    return best if score >= 2 else None


# --------------------------------------------------------------- fetch stage

# A search API's own extraction is used as-is above this many characters. Below
# it, the field is a relevance snippet rather than the page, and a real fetch is
# still needed. 2500 is comfortably above the snippet sizes measured (Tavily's
# default `content` came back 148-1356 chars) and below the full-page extractions
# (ollama.com returned 6k-11k on the same query).
PREFETCH_MIN_CHARS = 2500


def fetch_one(ev: Evidence, max_chars: int, cwd: Path) -> Evidence:
    if len(ev.prefetched) >= PREFETCH_MIN_CHARS:
        # Still harness-obtained text the model did not choose -- the design
        # property that matters is preserved. Skipping the round trip also
        # sidesteps the 403s, consent walls and JS shells that were failing
        # roughly half of all fetches.
        ev.text = ev.prefetched[:max_chars]
        ev.chars = len(ev.text)
        ev.norm = normalise(ev.text)
        ev.ok = True
        return ev
    try:
        if WORKER_FETCH is None:
            raise RuntimeError(f"worker import failed: {_WORKER_IMPORT_ERR}")
        text = WORKER_FETCH(cwd, {"url": ev.url}, max_chars=max_chars)
    except Exception as e:
        ev.error = f"{type(e).__name__}: {e}"[:200]
        return ev
    if not text or text.startswith("ERROR"):
        ev.error = (text or "empty")[:200]
        return ev
    ev.text = text
    ev.chars = len(text)
    ev.norm = normalise(text)
    # A page that yields a couple of sentences is a cookie wall or a JS shell.
    # Counting it as a source is exactly how a "12 sources" run turns out to
    # rest on three; better to record the failure than inflate the count.
    ev.ok = ev.chars >= 400
    if not ev.ok:
        ev.error = f"too short ({ev.chars} chars) -- likely a JS shell or consent wall"
    return ev


# --------------------------------------------------------------- vram guard

RESIDENCY_FILE = Path.home() / "bin" / "studio-research-residency.json"


def _canon_host(host: str) -> str:
    """Normalise a host URL so it matches KNOWN_OLLAMA_HOSTS.

    DEFAULT_HOST was `http://localhost:11434` while the table keys the Studio as
    `http://127.0.0.1:11434`, so a default run never matched the table and fell
    through to the local-sysctl branch -- reporting budget_src "local" instead of
    "studio". Harmless on this Mac by luck, and exactly the class of thing the
    single-source-of-truth rule exists to prevent.
    """
    h = (host or "").strip().rstrip("/")
    if not h:
        return h
    # Resolve an ALIAS ("unraid", "studio") to its URL. The queue passes --host
    # by alias -- its own status output prints `host=unraid` -- so alias support
    # is the common case, not an edge case.
    #
    # This was a REGRESSION I caused 2026-08-31: I made the budget check
    # fail-closed (refuse when capacity is unknown and something else is
    # resident) without making host identity robust, so `--host unraid` resolved
    # to "unknown-remote", budget 0, and a legitimate research job was REFUSED
    # while nomic-embed sat resident. Hardening a guard without hardening the
    # identity it looks up does not remove a failure, it swaps a fail-open hole
    # for a false refusal. Reported by the other session; it cost them a run.
    hosts = getattr(_worker, "KNOWN_OLLAMA_HOSTS", {}) if _worker else {}
    spec = hosts.get(h)
    if spec and spec.get("url"):
        return spec["url"].rstrip("/")
    if "://" not in h:
        h = "http://" + h
    h = h.replace("//localhost:", "//127.0.0.1:").replace("//::1:", "//127.0.0.1:")
    if not re.search(r":\d+$", h):          # default ollama port
        h = h + ":11434"
    # A hostname spelling of a known host ("unraid", "unraid.local") is the same
    # machine as its IP entry. Resolve and compare, so a DNS name is not treated
    # as an unknown remote and refused. Cheap, guarded, and never fatal.
    try:
        import socket, urllib.parse as _up
        pr = _up.urlsplit(h)
        if pr.hostname and not re.fullmatch(r"[\d.]+", pr.hostname):
            ip = socket.gethostbyname(pr.hostname)
            for spec in hosts.values():
                u = _up.urlsplit((spec.get("url") or "").rstrip("/"))
                if u.hostname == ip and (u.port or 11434) == (pr.port or 11434):
                    return spec["url"].rstrip("/")
    except Exception:
        pass
    return h


def pick_host(model: str, explicit: str | None = None) -> tuple[str, str]:
    """(url, why). Prefer Unraid; fall back to the Studio when the model cannot fit.

    Measured: Unraid scored 0.97 vs the Studio's 0.95 on the research bench and
    ran ~2.4x faster, so it is the better default for anything that fits its
    9.6GB usable budget. Anything larger has to go to the Studio's 44GB. The
    gate reads KNOWN_OLLAMA_HOSTS and the target's own model list (its /api/tags, via
    the worker's shared inventory -- see model_size_bytes) rather than a
    hardcoded size threshold, so it stays correct if either host changes.
    """
    if explicit:
        return _canon_host(explicit), "explicit --host"
    hosts = getattr(_worker, "KNOWN_OLLAMA_HOSTS", {}) if _worker else {}
    unraid = (hosts.get("unraid") or {}).get("url")
    studio = (hosts.get("studio") or {}).get("url")
    if not unraid or not studio:
        return _canon_host(DEFAULT_HOST), "hosts table incomplete; default"
    budget = int((hosts["unraid"] or {}).get("usable_bytes") or 0)
    size = model_size_bytes(unraid, model)
    if size <= 0:
        return studio, f"{model} not present on unraid; studio"
    kv = 1024 ** 3  # rough KV headroom; the gate only needs the obvious cases
    if size + kv <= budget:
        return unraid, (f"unraid: {model} is {size/1024**3:.1f}GB, fits the "
                        f"{budget/1024**3:.1f}GB budget (faster host)")
    return studio, (f"studio: {model} is {size/1024**3:.1f}GB, over unraid's "
                    f"{budget/1024**3:.1f}GB budget")


def gpu_budget_bytes(host: str) -> tuple[int, str]:
    """Usable GPU memory for the host we are actually dispatching to.

    This was a real bug and it cost a run. The first version read the LOCAL
    `iogpu.wired_limit_mb` / `hw.memsize`, which describes this Mac -- so when
    dispatched at Unraid's 12GB RTX 3080 it computed a 48GB budget, waved
    through a load alongside a resident 9.89GB qwen3:14b, and the job died mid
    -synthesis with `CUDA error: out of memory` after 66 findings of real work.
    A guard that is confidently wrong about a remote host is worse than no
    guard: it produces the collision it exists to prevent, and it does it after
    the expensive part.

    `ollama-worker.py` already publishes per-host usable_bytes, so use that as
    the single source of truth rather than maintaining a second table that can
    drift. Fall back to local sysctl ONLY when the target really is this
    machine, and say plainly when a remote host is unknown instead of guessing.
    """
    import subprocess
    hosts = getattr(_worker, "KNOWN_OLLAMA_HOSTS", {}) if _worker else {}
    h = _canon_host(host)
    for name, spec in hosts.items():
        if spec.get("url", "").rstrip("/") == h and spec.get("usable_bytes"):
            return int(spec["usable_bytes"]), name
    is_local = any(x in h for x in ("localhost", "127.0.0.1", "::1"))
    if not is_local:
        return 0, "unknown-remote"
    try:
        lim = int(subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"],
                                 capture_output=True, text=True,
                                 timeout=5).stdout.strip())
        if lim > 0:
            return lim * 1024 * 1024, "local"
    except Exception:
        pass
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                   capture_output=True, text=True,
                                   timeout=5).stdout.strip())
        return int(total * 0.75), "local"
    except Exception:
        return 48 * 1024 ** 3, "local-default"


def model_size_bytes(host: str, model: str) -> int:
    """The model's size on `host` in bytes, 0 when the host is unreachable or lacks it (exact
    name match, as before). Read from the worker's shared per-host model inventory
    (ollama-worker.py host_model_inventory): on Ollama 0.40.x every GET /api/tags parses GGUF
    metadata for every model off the array disks, so it is read once per OLLAMA_INVENTORY_TTL_S
    and shared with the queue daemon and the workers instead of once per call (2026-11-06).
    Falls back to the direct /api/tags read only if the worker module has no inventory."""
    inventory = getattr(globals().get("_worker"), "host_model_inventory", None)
    if inventory is not None:
        try:
            models = inventory(host.rstrip("/"), timeout=20)
            return int(models.get(model) or 0) if models is not None else 0
        except Exception:
            pass
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/tags")
        for m in json.load(urllib.request.urlopen(req, timeout=20)).get("models", []):
            if m.get("name") == model or m.get("model") == model:
                return int(m.get("size", 0))
    except Exception:
        pass
    return 0


def preflight_vram(host: str, model: str, num_ctx: int, force: bool) -> bool:
    """Refuse to load a model that will not fit alongside what is already
    resident.

    This exists because Studio GPU memory is shared with other Claude sessions
    running their own dispatches, and a previous cross-session collision OOM'd
    a run that had no way to see the other session's load. /api/ps is the
    ground truth for that -- better than any queue's own bookkeeping, because
    it reports what is physically resident regardless of who put it there.

    Refusing is the whole point: this never evicts another session's model.
    A model you did not load is a model someone else may be mid-run on.
    """
    # DARKBLOOM: it owns model residency/memory (MLX) -- /api/ps and /api/tags do not
    # exist there, and a refusal here would be a guess. Its own load-time memory check
    # (`darkbloom status`) is the authority.
    if _dbk.is_darkbloom(host):
        log("  vram preflight: skipped (Darkbloom manages residency)")
        return True
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/ps")
        ps = json.load(urllib.request.urlopen(req, timeout=20)).get("models", [])
    except Exception as e:
        log(f"  vram preflight: /api/ps unreachable ({e}) -- proceeding blind")
        return True
    resident = {m.get("name"): int(m.get("size", 0)) for m in ps}
    mine = resident.pop(model, None)
    others = sum(resident.values())
    want = model_size_bytes(host, model)
    # KV cache scales with context. ~0.5 GB per 32k is a deliberately rough
    # upper-ish estimate for a 30B-class MoE; the guard only needs to be right
    # about "does this obviously not fit", not to the megabyte.
    kv = int((num_ctx / 32768) * 0.5 * 1024 ** 3)
    budget, budget_src = gpu_budget_bytes(host)
    if budget == 0:
        # An unknown remote host used to proceed UNGUARDED here -- fail-open, so
        # adding a third host silently disabled the guard for it. That inverts
        # the lesson this function was written for. Capacity is unknown, but
        # /api/ps has already told us what is RESIDENT on the target, and that
        # is enough to decide the case that actually matters:
        #   nothing else resident -> no collision is possible -> proceed;
        #   something else resident -> a collision cannot be ruled out -> refuse.
        # Measure the target, and where capacity is unknowable, fall back to a
        # decidable question rather than to an assumption.
        # Tolerate a trivially small co-resident model. The guard exists to stop
        # a real collision on a 12GB card; refusing because a 0.46GB embedding
        # model is loaded is the over-caution the other session hit. Anything
        # this small cannot be what pushes a load over the edge.
        SMALL_RESIDENT = 1024 ** 3
        if others and others <= SMALL_RESIDENT and mine is None:
            log(f"  vram preflight: {host} has no known VRAM budget; "
                f"{others/1024**3:.2f}GB resident ({', '.join(resident)}) is small "
                f"enough to ignore -- proceeding.")
        elif others and mine is None:
            log(f"  vram preflight: {host} has no known VRAM budget AND "
                f"{others/1024**3:.1f}GB is already resident "
                f"({', '.join(resident)}). Cannot prove this fits, and will not "
                f"evict another session's model. Add it to "
                f"ollama-worker.py's KNOWN_OLLAMA_HOSTS, or pass --force.")
            if not force:
                return False
        else:
            log(f"  vram preflight: {host} has no known VRAM budget, but nothing "
                f"else is resident -- no collision possible, proceeding. Add it "
                f"to ollama-worker.py's KNOWN_OLLAMA_HOSTS for a real check.")
        return True
    need = want + kv
    G = 1024 ** 3
    if mine is not None:
        log(f"  vram preflight: {model} already resident "
            f"({mine/G:.1f}GB) -- no new load needed")
    if others:
        log(f"  vram preflight: {len(resident)} other model(s) resident, "
            f"{others/G:.1f}GB: {', '.join(resident)}")
    log(f"  vram preflight [{budget_src}]: need ~{need/G:.1f}GB "
        f"(weights {want/G:.1f} + kv {kv/G:.1f}), "
        f"other sessions hold {others/G:.1f}GB, budget {budget/G:.1f}GB")
    if mine is None and others + need > budget:
        msg = (f"ABORT: {model} needs ~{need/G:.1f}GB but only "
               f"{(budget-others)/G:.1f}GB is free -- "
               f"{', '.join(resident) or 'another process'} is holding "
               f"{others/G:.1f}GB. Not evicting it: another session may be "
               f"mid-run. Wait, coordinate, or pass --force.")
        if force:
            log(msg.replace("ABORT", "WARNING (--force given, continuing)"))
        else:
            log(msg)
            return False
    try:
        RESIDENCY_FILE.write_text(json.dumps({
            "owner": "studio-research.py", "pid": __import__("os").getpid(),
            "model": model, "num_ctx": num_ctx,
            "approx_bytes": need, "since": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    except Exception:
        pass  # advisory only -- never fail a run over the courtesy file
    return True


# ------------------------------------------------------------------- schemas

# NOTE: there is deliberately no free-text "interpretation" field here. There
# was one, and with thinking disabled the model used it as a reasoning sink:
# OpenResearcher-30B filled 900+ characters of it with chain-of-thought ("I
# recall that the RTX 3080 launched with 10GB... I think it never
# materialized"), then emitted "queries": [] having spent the budget. A
# free-text field in a structured-output schema is where suppressed reasoning
# goes; the fix is not to provide one. Its only use was a log line.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_questions": {
            "type": "array", "maxItems": 7, "minItems": 3,
            "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "question": {"type": "string"},
                "why": {"type": "string"}},
                "required": ["id", "question"]}},
        "queries": {
            "type": "array", "maxItems": 18, "minItems": 4,
            "items": {"type": "object", "properties": {
                "sub_question_id": {"type": "string"},
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["general", "academic"]}},
                "required": ["sub_question_id", "query"]}},
    },
    "required": ["sub_questions", "queries"],
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object", "properties": {
                "sub_question_id": {"type": "string"},
                "quote": {"type": "string"},
                "statement": {"type": "string"}},
                "required": ["sub_question_id", "quote", "statement"]}},
    },
    "required": ["findings"],
}

GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {
            "type": "array", "maxItems": 7,
            "items": {"type": "object", "properties": {
                "sub_question_id": {"type": "string"},
                "answered": {"type": "boolean"},
                "what_is_missing": {"type": "string"}},
                "required": ["sub_question_id", "answered"]}},
        "queries": {
            "type": "array", "maxItems": 12,
            "items": {"type": "object", "properties": {
                "sub_question_id": {"type": "string"},
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["general", "academic"]}},
                "required": ["sub_question_id", "query"]}},
    },
    "required": ["assessment", "queries"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string",
                    "enum": ["SUPPORTED", "PARTIAL", "UNSUPPORTED"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}


# ------------------------------------------------------------------- prompts

PLAN_SYS = """You are the planning stage of a research pipeline. You do not \
have web access and you are not answering the question -- another stage does \
that. Your only job is to decide what would have to be true for an answer to \
be well-founded, and to write the searches that would establish it.

Decompose the question into 4 to 7 sub-questions. A good sub-question is one \
a source can actually settle: it names a specific fact, number, date, policy, \
mechanism or comparison. Split compound questions apart -- if the question \
asks whether a thing exists AND what it costs, those are two sub-questions, \
because a source can answer one and not the other.

Include a sub-question for the load-bearing assumption behind the question \
itself when there is one. If asked "how much does X's premium tier cost", \
whether a premium tier exists at all is a real sub-question, and the answer \
may be that it does not.

Then write 2 or 3 search queries per sub-question. Vary them: a phrase a \
primary source would use (vendor docs, a spec sheet, a filing), a phrase a \
secondary source would use (a review, a comparison), and where useful an \
exact-phrase or site-specific query. Do not simply restate the sub-question \
as a query -- search engines match documents, not questions. Use kind \
"academic" only for queries where a paper or preprint is the right kind of \
source."""

EXTRACT_SYS = """You are the extraction stage of a research pipeline. You are \
given the text of ONE web page and a list of sub-questions. Report only what \
this page actually says.

For each sub-question this page genuinely bears on, emit a finding:
  - "quote": text copied VERBATIM from the page, character for character, \
25-300 characters, containing the fact itself. Copy it; do not retype it from \
memory, do not tidy the grammar, do not translate it, do not merge two \
separate sentences into one quote.
  - "statement": one sentence, in your own words, saying what that quote \
establishes for that sub-question.

Every quote is checked automatically against the real page text. A quote that \
is not literally present is discarded along with its statement, so an invented \
quote does not get you a finding -- it loses you one.

Emit nothing for sub-questions this page does not address. A page that \
addresses none of them should return an empty findings list; that is a normal \
and useful outcome, not a failure. Do not pad. Do not use knowledge you \
already have -- if the page does not say it, it is not a finding.

Prefer specifics: a page that gives a number, a date, a version, a price, or a \
named limitation is worth more than one that gestures at the topic."""

GAP_SYS = """You are the sufficiency check of a research pipeline. You are \
given the sub-questions and every finding gathered so far, each tagged with \
the source it came from.

For each sub-question decide honestly whether the findings actually settle it. \
Be strict. A sub-question is NOT answered when:
  - only one source supports it and the claim is specific (a number, a price, \
a date) -- specifics need corroboration;
  - the sources disagree and nothing resolves which is right;
  - the findings are about the general topic but never state the specific fact \
asked for;
  - the only support is a vendor's own marketing page for a claim about that \
vendor's limitations.

Then write new search queries targeting exactly what is missing. Queries that \
already ran will not be re-run, so write different ones: more specific, \
different vocabulary, a different kind of source, or aimed at the disagreement \
rather than the topic. If a sub-question looks genuinely unanswerable from the \
open web, say so in what_is_missing and write no query for it -- a wasted round \
is worse than an honest gap.

If everything is genuinely settled, return an empty query list."""

SYNTH_SYS = """You are the synthesis stage of a research pipeline. Write the \
final answer to the research question using ONLY the findings supplied. Each \
finding carries an evidence id like [E7].

Rules:
  - Every sentence containing a specific fact -- a number, date, name, price, \
version, quantity, or a claim that something is or is not the case -- must end \
with the evidence ids supporting it, like [E7] or [E3][E12].
  - Cite the id that the finding you are using actually carries. Do not attach a \
fact to a source that did not state it, even when you are confident the fact is \
right and some other source did state it. Each id is checked against that \
source's own quotes afterwards, so a fact cited to the wrong source is reported \
as unsupported even though it is true. If two findings support one sentence, \
cite both ids.
  - Use no fact that is not in the findings, however confident you are of it. \
Your own prior knowledge is not evidence here and must not appear.
  - Where sources disagree, say so explicitly, give both figures with their \
ids, and say which is better supported and why. Do not average them, do not \
silently pick one.
  - Lead with the direct answer to the question asked, then the supporting \
detail. Do not open with a restatement of the question or a description of \
your process.
  - End with a section "## Not established" listing every sub-question the \
evidence did not settle, and what specifically is missing. If a load-bearing \
assumption in the question turned out to be false, say that plainly and early \
-- that is the answer, not a failure to answer.

Write in prose and short sections. No preamble, no "as an AI", no summary of \
what you are about to do. Markdown."""

VERIFY_SYS = """You are the verification stage of a research pipeline, and you \
are adversarial. You did not write the sentence you are checking and you owe it \
nothing.

You are given one sentence from a draft report and the verbatim quotes it \
cites. Decide whether those quotes actually establish that sentence.

  SUPPORTED   -- the quotes state this, or it follows from them immediately.
  PARTIAL     -- the quotes support part of it, or support something weaker. \
Use this when the sentence generalises beyond the quote, drops a condition the \
quote attaches, changes a hedge into a certainty, or states a number the quote \
gives for a different thing.
  UNSUPPORTED -- the quotes do not establish it. Includes: the sentence is \
true in the world but these quotes do not show it; the quotes are about a \
related but different entity, product, tier, region or year.

Judge the sentence against the quotes alone. Do not use what you know. A \
sentence you believe is correct is still UNSUPPORTED if these quotes do not \
show it -- that is the finding, not an error."""


_INTERROGATIVE = re.compile(
    r"\b(what|when|where|which|who|whose|why|how|does|do|did|is|are|was|were|"
    r"can|could|has|have|will|should)\b", re.I)


def looks_like_a_question(text: str) -> bool:
    """Reject a 'sub-question' that is really a search query.

    Live failure: asked for sub-questions AND queries in one object, the planner
    filled both arrays with query strings -- sub_questions came back as
    '"RTX 3080 12GB" existence', '"RTX 3080 12GB" release date', and so on. The
    extraction stage is then asked "which of these sub-questions does this page
    bear on", is handed a list of keyword strings, and correctly answers "none"
    for every page. Nine good sources, zero findings, no error anywhere.

    A real sub-question is a sentence with an interrogative in it, not a bag of
    keywords, and it is long enough to say what it wants.
    """
    t = (text or "").strip()
    if len(t) < 25:
        return False
    if t.startswith('"') and t.count('"') >= 2 and len(t) < 60:
        return False          # a quoted search phrase with a word bolted on
    return bool(_INTERROGATIVE.search(t)) or t.endswith("?")


def rank_chunks(text: str, subqs: list[dict], size: int, k: int) -> list[str]:
    """Split a page into windows and return the k most relevant to the
    sub-questions, best first.

    Taking the first N characters of every page -- the obvious thing -- is a
    systematic thoroughness bug, not a rounding error: the lead of a long
    document is boilerplate, navigation and framing, while the fact being
    researched sits in a spec table, a pricing section or a changelog far down.
    A harness that only ever reads the top of the page will reliably miss
    exactly the specific, quotable numbers that separate a thorough answer from
    a plausible one.

    Scoring is deliberately lexical rather than embedding-based: it needs no
    model call, no second model resident in VRAM, and it is the sub-questions'
    own distinctive words that matter here. Rare words count for more than
    common ones, which is what stops a chunk from winning by repeating "the
    card" forty times.
    """
    if len(text) <= size:
        return [text]
    step = max(1, size - 600)          # overlap, so a fact on a seam survives
    windows = [text[i:i + size] for i in range(0, len(text), step)]
    windows = [w for w in windows if len(w) > 200]
    terms: dict[str, int] = {}
    for q in subqs:
        for w in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", q["question"].lower()):
            terms[w] = terms.get(w, 0) + 1
    if not terms:
        return windows[:k]
    # A term appearing in every chunk carries no signal about which chunk to read.
    df = {t: sum(1 for w in windows if t in w.lower()) or 1 for t in terms}
    scored = []
    for idx, w in enumerate(windows):
        low = w.lower()
        score = sum((len(re.findall(re.escape(t), low)) ** 0.5)
                    * (len(windows) / df[t]) for t in terms)
        # Mild preference for earlier chunks only as a tie-break: leads do carry
        # the summary when a page is short on distinctive vocabulary.
        scored.append((score - idx * 0.01, idx, w))
    scored.sort(reverse=True)
    return [w for _, _, w in scored[:k]]


def queries_from_subquestions(subqs: list[dict], have: list[dict],
                             topic: str = "") -> list[dict]:
    """Deterministic fallback queries, one per sub-question that has none.

    A planner can return good sub-questions and an empty query list -- observed
    live on OpenResearcher-30B, which produced three well-formed sub-questions
    and zero queries, and the run aborted with "no queries left" having done no
    searching at all. Losing a whole research run because one field of one JSON
    object came back empty is a harness failure, not a model failure: the
    sub-question text is itself a serviceable query.

    Strips the interrogative framing, since search engines match documents
    rather than questions, and re-anchors the query to the subject of the
    original question. Sub-questions lean on anaphora -- "If a 12 GB version
    exists, when was it released?" reduces to "12 GB version released date",
    which names no product and searches for nothing. The distinctive terms of
    the parent question (proper nouns, model numbers) are prepended when the
    sub-question has dropped them.
    """
    # The first word of a sentence is capitalised regardless of whether it is a
    # proper noun, and the questions here start with interrogatives. Taking
    # capitalisation at face value put "Does" at the front of a derived query,
    # which returned the Merriam-Webster and Dictionary.com entries for the word
    # "does" as research sources. Anchors must be words that are distinctive,
    # not words that happen to be first.
    _skip = {"does", "do", "did", "is", "are", "was", "were", "what", "when",
             "which", "who", "why", "how", "can", "could", "has", "have",
             "will", "should", "if", "the", "a", "an", "and", "or", "in", "on",
             "of", "for", "to", "it", "its", "there", "this", "that"}
    anchor = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", topic)
              if (w[0].isupper() or any(c.isdigit() for c in w))
              and len(w) > 1 and w.lower() not in _skip]
    seen_anchor, anchors = set(), []
    for w in anchor:                      # de-dupe, keep order, cap the length
        if w.lower() not in seen_anchor:
            seen_anchor.add(w.lower())
            anchors.append(w)
    anchors = anchors[:4]
    covered = {q.get("sub_question_id") for q in have}
    stop = {"does", "do", "did", "is", "are", "was", "were", "what", "when",
            "which", "who", "whom", "whose", "why", "how", "the", "a", "an",
            "of", "in", "on", "for", "to", "or", "and", "if", "it", "its",
            "there", "any", "exist", "exists", "have", "has", "be", "been",
            "that", "this", "these", "those", "only", "also", "with"}
    out = []
    for sq in subqs:
        if sq["id"] in covered:
            continue
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", sq["question"])
        kept = [w for w in words if w.lower() not in stop]
        low = {w.lower() for w in kept}
        missing = [a for a in anchors if a.lower() not in low]
        query = " ".join(missing + kept[:12]).strip(" ,.?")
        if len(query) >= 8:
            out.append({"sub_question_id": sq["id"], "query": query,
                        "kind": "general"})
    return out


# ------------------------------------------------------------------- stages

# --------------------------------------------------------------- search chain

def _keychain(service: str) -> str | None:
    """Read a secret from Keychain. Never logged, never written to disk."""
    import subprocess, os
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
             "-s", service, "-w"], capture_output=True, text=True, timeout=10)
        k = r.stdout.strip()
        return k or None
    except Exception:
        return None


def _post_json(url: str, body: dict, headers: dict, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def search_ollama(query: str, want: int) -> list[dict]:
    """ollama.com web search. Free tier, and a REAL API rather than scraped SERPs.

    Returns full page CONTENT, not a snippet -- measured 6k-11k characters per
    result against Tom's Hardware, VideoCardz and TechPowerUp. That matters
    beyond convenience: content the search API already extracted is content the
    harness does not have to re-fetch, which sidesteps the 403s, consent walls
    and JS shells that were failing ~50% of fetches and silently halving every
    run's evidence base.
    """
    k = _keychain("ollama-web-search")
    if not k:
        return []
    d = _post_json("https://ollama.com/api/web_search",
                   {"query": query, "max_results": want},
                   {"Authorization": f"Bearer {k}"})
    out = []
    for r in d.get("results", []):
        out.append({"url": r.get("url", ""), "title": str(r.get("title", "")),
                    "content": str(r.get("content", "") or ""),
                    "engine": "ollama"})
    return out


def search_tavily(query: str, want: int) -> list[dict]:
    """Tavily. Free tier, 1000 credits/month, no credit card.

    `include_raw_content` is requested because the default `content` field is a
    short relevance snippet (measured 148-1356 chars); the raw extraction is
    what makes a result usable as evidence without a second fetch.
    """
    k = _keychain("tavily-api")
    if not k:
        return []
    global TAVILY_CALLS
    TAVILY_CALLS += 1
    d = _post_json("https://api.tavily.com/search",
                   {"query": query, "max_results": want,
                    "include_raw_content": True},
                   {"Authorization": f"Bearer {k}"})
    out = []
    for r in d.get("results", []):
        body = r.get("raw_content") or r.get("content") or ""
        out.append({"url": r.get("url", ""), "title": str(r.get("title", "")),
                    "content": str(body), "engine": "tavily"})
    return out


def search_searxng(searxng: str, query: str, engines: str, want: int,
                   health: dict) -> list[dict]:
    results, unresponsive = searxng_search(searxng, query, engines, want)
    for name, reason in unresponsive:
        health.setdefault(name, set()).add(str(reason)[:40])
    return [{"url": r.get("url", ""), "title": r.get("title", ""),
             "content": r.get("content", "") or "", "engine": r.get("engine", "searxng")}
            for r in results]


def search_chain(query: str, want: int, searxng: str, engines: str,
                 health: dict, budget: dict | None = None) -> tuple[list[dict], str]:
    """ollama.com -> Tavily -> SearXNG, first one that answers wins.

    ORDER IS ABOUT COST, not quality. Tavily is the better result (raw_content
    returned 20k-75k characters of full page text against ollama.com's 6k-11k),
    but its free tier is a **capped budget: 1,000 credits/month**, and this
    harness issues 15-25 searches per research run. Putting Tavily first spends
    that month of credits in roughly 40-60 runs -- fewer if raw_content costs
    more than one credit, which is unverified. ollama.com's free tier is not
    metered the same way and also returns full page content, so it does the
    bulk work and Tavily is held as the quality reserve for when ollama.com
    fails or returns nothing.

    Brave is deliberately NOT in this chain at all. Its free tier was killed in
    February 2026: it bills $5 per 1,000 requests against a card that Brave's own
    FAQ previously said would never be charged, and its $5 monthly credit is
    conditional on publicly attributing Brave. A paid fallback nobody notices
    firing is exactly how a bill accumulates quietly. The key exists in Keychain
    as a deliberate last resort; using it should be an explicit decision.

    `budget` caps Tavily calls for a single run so one research question cannot
    eat a meaningful share of the month.
    """
    def _try(name, fn):
        try:
            res = [r for r in fn() if r.get("url", "").startswith("http")]
            if res:
                return res
            health.setdefault(name, set()).add("returned no results")
        except Exception as e:
            health.setdefault(name, set()).add(f"{type(e).__name__}: {str(e)[:40]}")
        return None

    res = _try("ollama", lambda: search_ollama(query, want))
    if res:
        return res, "ollama"

    if budget is not None and budget.get("tavily", 0) <= 0:
        health.setdefault("tavily", set()).add("per-run credit budget exhausted")
    else:
        res = _try("tavily", lambda: search_tavily(query, want))
        if res:
            if budget is not None:
                budget["tavily"] = budget.get("tavily", 0) - 1
            return res, "tavily"

    return search_searxng(searxng, query, engines, want, health), "searxng"


def topic_terms(topic: str) -> tuple[set, set]:
    """Split the question's vocabulary into STRONG and WEAK terms.

    Strong: proper nouns and anything containing a digit -- NVIDIA, RTX, 3080,
    12GB. These name the subject.
    Weak: merely long words -- "configuration", "released", "original",
    "variant". These are about the subject but do not identify it.

    The distinction is load-bearing. Treating any long word as distinctive let
    thesaurus.com's "Do vs. Does", vocabulary.com's entry for "release" and
    Collins' entry for "release" all pass the relevance filter on a question
    about a graphics card -- they mention "original" and "release", which is
    enough under a flat rule and says nothing at all. With one search engine
    left answering, junk like that stops being a nuisance and becomes most of
    the round.
    """
    skip = {"does", "do", "did", "is", "are", "was", "were", "what", "when",
            "which", "who", "why", "how", "can", "could", "has", "have",
            "will", "should", "the", "and", "for", "with", "from", "that",
            "this", "there", "its", "only", "any", "exist", "exists"}
    strong, weak = set(), set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", topic):
        wl = w.lower()
        if wl in skip or len(w) < 3:
            continue
        if w[0].isupper() or any(c.isdigit() for c in w):
            strong.add(wl)
        elif len(w) >= 6:
            weak.add(wl)
    return strong, weak


def result_is_relevant(result: dict, terms: tuple[set, set]) -> bool:
    """Cheap on-topic check for a search result, before it costs a fetch.

    Requires ONE strong term (a proper noun or something with a digit), which is
    the term that actually names the subject. Weak terms alone are not enough --
    a page can say "original" and "release" and be a dictionary entry. When a
    question happens to contain no strong terms at all, fall back to requiring
    two weak ones rather than letting everything through.

    Still deliberately permissive: this drops results about a different subject,
    it does not rank the ones about this subject. That is extraction's job, and
    extraction works from the page rather than a truncated snippet.
    """
    strong, weak = terms
    if not strong and not weak:
        return True
    # Title, snippet, and the URL PATH -- never the bare domain. Raytheon's
    # rtx.com matched "rtx" on its hostname alone and was fetched as a source
    # for a question about the GeForce RTX 3080; its title says only "Raytheon
    # Technologies". A domain is a name, not a statement about content.
    path = urllib.parse.urlparse(result.get("url", "")).path
    hay = " ".join([result.get("title", ""), result.get("content", ""),
                    path]).lower()
    if strong:
        return any(t in hay for t in strong)
    return sum(1 for t in weak if t in hay) >= 2



_ACADEMIC_TOPIC = re.compile(
    r"\b(paper|papers|preprint|arxiv|journal|study|studies|literature|citation|"
    r"peer.reviewed|publication|thesis|conference|proceedings|clinical|trial|"
    r"algorithm|theorem|dataset|benchmark|state.of.the.art)\b", re.I)


def stage_gather(searxng: str, queries: list[dict], store: dict, seen_urls: set,
                 seen_queries: set, per_query: int, max_new: int, max_chars: int,
                 cwd: Path, workers: int, engine_health: dict,
                 topic: str = "", tavily_budget: dict | None = None) -> list[Evidence]:
    """Search, dedupe, fetch. The model is not in this loop at all."""
    # Every query is run before anything is dropped, and the source budget is
    # then filled ROUND-ROBIN across queries. Filling it in query order instead
    # (the obvious loop) spent the entire budget on query #1 in a live run: 5 of
    # 5 sources came from the first two queries and four of the six sub-questions
    # were never searched for at all. Coverage across sub-questions is the whole
    # point of having decomposed the question, so it cannot be left to whichever
    # query happened to be listed first.
    per_query_hits: list[list[Evidence]] = []
    backends_used: set = set()
    degraded = [0]          # engines unresponsive on the previous search
    terms = topic_terms(topic)
    offtopic = 0
    pending = 0
    for q in queries:
        query = (q.get("query") or "").strip()
        if not query or normalise(query) in seen_queries:
            continue
        seen_queries.add(normalise(query))
        # Academic engines are added to the general set, never substituted for it.
        # Live failure: the planner tagged 'site:nvidia.com "RTX 3080" 12GB' as
        # academic, the general engines were skipped, and all five sources came
        # back as unrelated arXiv preprints -- a consumer-GPU question answered
        # entirely from a paper index.
        # The planner's "academic" tag is honoured only when the QUESTION is
        # academic, not merely because the planner said so. It tagged
        # `site:nvidia.com "RTX 3080" 12GB` academic on a consumer-GPU question;
        # arXiv then answered where the rate-limited general engines could not,
        # and all twelve sources for that round came back as preprints. The
        # paper indexes are prolific and always available, so on a non-academic
        # question they don't supplement the general engines, they replace them.
        use_academic = (q.get("kind") == "academic"
                        and bool(_ACADEMIC_TOPIC.search(topic)))
        engines = (GENERAL_ENGINES + "," + ACADEMIC_ENGINES
                   if use_academic else GENERAL_ENGINES)
        # Deliberate pacing. Firing ten queries at a self-hosted metasearch in
        # four seconds is what turns "3 engines down" into "7 engines down" --
        # measured across this session, brave and google cse both went from
        # answering to rate-limited under exactly this pattern, and thin search
        # is indistinguishable in the output from a thin answer. Search is not
        # the slow part of a research run; the model is.
        if per_query_hits:
            time.sleep(SEARCH_PACE_S)
        # Adaptive breadth. When most general engines are suspended, the one
        # still answering returns the SAME top results for every query, so a
        # fixed per-query cap makes later queries contribute nothing -- observed
        # live as a run of "8 results, 0 new" lines once 5 of 6 engines were
        # rate-limited. Widening the take from the survivor is the only lever
        # left at that point, and it costs nothing: the results are already in
        # the response, they were being discarded.
        want = per_query
        if degraded[0] >= 3:
            want = min(per_query * 3, 24)
        results, backend = search_chain(query, want, searxng, engines,
                                        engine_health, tavily_budget)
        # Only the scraped backend degrades under load; the APIs do not, so the
        # adaptive-breadth widening is pointless (and wasteful) unless we fell
        # all the way through to SearXNG.
        degraded[0] = 0 if backend != "searxng" else degraded[0]
        backends_used.add(backend)
        hits: list[Evidence] = []
        for r in results:
            url = (r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            if not result_is_relevant(r, terms):
                offtopic += 1
                continue
            if JUNK_HOSTS.search(urllib.parse.urlparse(url).netloc):
                continue
            key = url.split("#")[0].rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            ev = Evidence("", url, r.get("title", ""), query,
                          r.get("engine", ""))
            ev.prefetched = r.get("content", "") or ""
            hits.append(ev)
        pending += len(hits)
        per_query_hits.append(hits)
        log(f"  search[{backend}]: {query!r} -> {len(results)} results, "
            f"{len(hits)} new")

    # Round-robin across queries, with a per-domain cap.
    #
    # The cap is not tidiness. Live run: with 5 of 6 general engines rate-limited
    # and several queries tagged "academic", arXiv answered where the general
    # engines could not, and ALL TWELVE sources for a consumer-graphics-card
    # question came back as arXiv preprints. Every one extracted zero findings,
    # correctly -- they were real pages that simply had nothing to do with the
    # question. One prolific engine drowning the pool looks identical in the log
    # to a successful gather, and it wastes the entire round's fetch budget.
    #
    # Two passes: take at most DOMAIN_CAP per domain first, then re-admit the
    # overflow at the end, so a domain that genuinely is the best source (a
    # vendor's own docs) still gets read once the field has been given its
    # chance.
    if offtopic:
        log(f"  dropped {offtopic} search result(s) mentioning nothing from the "
            f"question")
    ranked: list[Evidence] = []
    overflow: list[Evidence] = []
    per_domain: dict[str, int] = {}
    for rank in range(max((len(h) for h in per_query_hits), default=0)):
        for hits in per_query_hits:
            if rank >= len(hits):
                continue
            ev = hits[rank]
            dom = urllib.parse.urlparse(ev.url).netloc.lower().removeprefix("www.")
            if per_domain.get(dom, 0) >= DOMAIN_CAP:
                overflow.append(ev)
                continue
            per_domain[dom] = per_domain.get(dom, 0) + 1
            ranked.append(ev)
    ranked += overflow
    if not ranked:
        return []
    top = sorted(per_domain.items(), key=lambda kv: -kv[1])[:3]
    if top and top[0][1] >= DOMAIN_CAP:
        log(f"  domain mix: {', '.join(f'{d}x{n}' for d, n in top)}"
            f"{f' (+{len(overflow)} held back by the per-domain cap)' if overflow else ''}")

    # Fetch in WAVES until max_new sources actually succeed, rather than
    # fetching exactly max_new candidates and keeping whatever survives.
    # Measured on a live run: 68 candidates found, 12 fetched, 5 usable -- a 42%
    # success rate, because a search result set is full of JS shells, 403s and
    # consent walls. Sizing the fetch to the candidate count rather than the
    # success count silently halves every run's evidence base, and it does it
    # invisibly: the log says "12 sources" and the report rests on 5.
    good: list[Evidence] = []
    next_eid = [len(store)]
    attempted = 0
    idx = 0
    while len(good) < max_new and idx < len(ranked):
        wave = ranked[idx:idx + max(4, (max_new - len(good)) * 2)]
        idx += len(wave)
        # Monotonic, never derived from len(store). Deriving it collided:
        # wave 1 hands out E1..E24 but only the fetched ones enter the store, so
        # wave 2 computing len(store)+1 restarts inside a range wave 1 already
        # issued, and `store[e.id] = e` then silently overwrites a real source
        # with a later one. Evidence ids are cited in the report, so a collision
        # does not just lose a source -- it repoints a citation at the wrong page.
        for ev in wave:
            next_eid[0] += 1
            ev.id = f"E{next_eid[0]}"
        log(f"  fetching {len(wave)} source(s), {workers} workers "
            f"({len(good)}/{max_new} usable so far, {len(ranked) - idx} held back)")
        with cf.ThreadPoolExecutor(workers) as pool:
            fetched = list(pool.map(lambda e: fetch_one(e, max_chars, cwd), wave))
        attempted += len(fetched)
        for e in fetched:
            if e.ok and len(good) < max_new:
                store[e.id] = e
                good.append(e)
            elif not e.ok:
                store[e.id] = e
                log(f"    {e.id} FAILED {e.url[:66]} -- {e.error[:64]}")
    prefetched = len([e for e in good if e.prefetched])
    log(f"  fetched OK: {len(good)} usable this round "
        f"from {attempted} fetch attempt(s)"
        + (f"; {prefetched} came with full text from the search API"
           if prefetched else ""))
    stage_gather.last_backends = backends_used
    return good


def stage_extract(model: Model, sources: list[Evidence], subqs: list[dict],
                  extract_chars: int, stats: dict,
                  chunks_per_source: int = 2) -> list[dict]:
    """Map the model over each fetched source. Quotes are verified against the
    real page text here, which is the point at which fabrication dies."""
    subq_block = "\n".join(f"{s['id']}: {s['question']}" for s in subqs)
    findings: list[dict] = []
    for ev in sources:
        chunks = rank_chunks(ev.text, subqs, extract_chars, chunks_per_source)
        kept = 0
        seen_quotes: set = set()
        for ci, text in enumerate(chunks):
            user = (f"SUB-QUESTIONS:\n{subq_block}\n\n"
                    f"PAGE URL: {ev.url}\nPAGE TITLE: {ev.title}\n"
                    + (f"(section {ci+1} of {len(chunks)} read from this page)\n"
                       if len(chunks) > 1 else "")
                    + f"\nPAGE TEXT:\n{text}\n\n"
                    f"Emit findings for the sub-questions this page actually addresses.")
            # temperature 0, not the run's default. Measured on
            # OpenResearcher-30B against one page, three repeats each: at
            # temperature 0.3 extraction returned 1, 0, 0 findings, the empty
            # ones coming back in 0.3s -- the model sampling straight into
            # `{"findings": []}`, the shortest string the grammar accepts. At
            # temperature 0.0 the same call returned 4, 4, 4. Under a constrained
            # grammar the degenerate empty answer is always available and always
            # cheap, so any sampling at all keeps finding it. Extraction is
            # transcription, not writing; it has nothing to gain from sampling.
            out = model.chat_json(EXTRACT_SYS, user, EXTRACT_SCHEMA,
                                  temperature=0.0, num_predict=2000)
            if not out:
                stats["extract_parse_fail"] += 1
                log(f"    {ev.id} PARSE FAILURE on section {ci+1} "
                    f"-- model returned nothing usable")
                continue
            for f in (out.get("findings") or []):
                quote = (f.get("quote") or "").strip()
                sid = (f.get("sub_question_id") or "").strip()
                stmt = (f.get("statement") or "").strip()
                if not quote or not stmt:
                    continue
                # Verify the QUOTE first: that is the claim about reality. The
                # id is only a filing decision and must never cost real evidence.
                if not verify_quote(quote, ev):
                    stats["quotes_rejected"] += 1
                    continue
                key = normalise(quote)[:80]
                if key in seen_quotes:      # overlapping windows see it twice
                    continue
                seen_quotes.add(key)
                mapped = assign_subq(sid, stmt, subqs)
                if mapped is None:
                    stats["bad_subq_id"] += 1
                    mapped = subqs[0]["id"]  # a mis-filed fact still counts
                elif mapped != sid:
                    stats["subq_remapped"] += 1
                findings.append({"evidence_id": ev.id, "sub_question_id": mapped,
                                 "quote": quote, "statement": stmt, "url": ev.url})
                kept += 1
                stats["quotes_kept"] += 1
        log(f"    {ev.id} {ev.url[:56]:56s} -> {kept} finding(s)"
            + (f" from {len(chunks)} sections" if len(chunks) > 1 else ""))
    return findings


def stage_gap(model: Model, question: str, subqs: list[dict],
              findings: list[dict], store: dict) -> dict | None:
    by_sub: dict[str, list[dict]] = {}
    for f in findings:
        by_sub.setdefault(f["sub_question_id"], []).append(f)
    blocks = []
    for s in subqs:
        fs = by_sub.get(s["id"], [])
        srcs = {f["evidence_id"] for f in fs}
        lines = [f"{s['id']}: {s['question']}",
                 f"  findings: {len(fs)} from {len(srcs)} distinct source(s)"]
        for f in fs[:8]:
            lines.append(f"  - [{f['evidence_id']}] {f['statement']}")
        if not fs:
            lines.append("  - (nothing found)")
        blocks.append("\n".join(lines))
    user = (f"RESEARCH QUESTION: {question}\n\n"
            f"SUB-QUESTIONS AND FINDINGS SO FAR:\n\n" + "\n\n".join(blocks))
    # Greedy for the same reason as extraction: an empty query list is the
    # cheapest thing the grammar allows, and sampling finds it.
    return model.chat_json(GAP_SYS, user, GAP_SCHEMA, temperature=0.0,
                           num_predict=2000)


def stage_synth(model: Model, question: str, subqs: list[dict],
                findings: list[dict], store: dict) -> str:
    by_sub: dict[str, list[dict]] = {}
    for f in findings:
        by_sub.setdefault(f["sub_question_id"], []).append(f)
    blocks = []
    for s in subqs:
        fs = by_sub.get(s["id"], [])
        lines = [f"### {s['id']}: {s['question']}"]
        if not fs:
            lines.append("(no evidence found for this sub-question)")
        for f in fs:
            lines.append(f"[{f['evidence_id']}] {f['statement']}\n"
                         f"      quote: \"{f['quote']}\"\n"
                         f"      source: {f['url']}")
        blocks.append("\n".join(lines))
    user = (f"RESEARCH QUESTION: {question}\n\n"
            f"FINDINGS (every one verified as a real quote from a real fetched "
            f"page):\n\n" + "\n\n".join(blocks) +
            f"\n\nWrite the final answer to the research question.")
    return model.chat(SYNTH_SYS, user, temperature=0.3, num_predict=4000)


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_CITE = re.compile(r"\[E\d+\]")


def stage_verify(model: Model, report: str, findings: list[dict],
                 stats: dict) -> list[dict]:
    """Adversarially check each cited sentence against the quotes it cites."""
    quotes: dict[str, list[dict]] = {}
    for f in findings:
        quotes.setdefault(f["evidence_id"], []).append(f)
    # The "Not established" section is excluded. Its sentences are claims about
    # the EVIDENCE SET ("the specific day is not detailed in the findings"), not
    # claims about the world, so checking them against quotes is a category
    # error -- and it fired three times out of six failures on a live run,
    # dragging grounding from 10/13 down to 7/13 for sentences that were doing
    # exactly what the report is supposed to do. A sentence saying a fact is
    # missing cannot be supported by a quote containing that fact.
    checks: list[dict] = []
    body = re.split(r"^##\s*Not established", report, flags=re.I | re.M)[0]
    for line in body.splitlines():
        if line.strip().startswith("#") or not line.strip():
            continue
        for sent in _SENT_SPLIT.split(line):
            ids = _CITE.findall(sent)
            if not ids:
                continue
            eids = [i.strip("[]") for i in ids]
            ev_block = []
            for eid in eids:
                for f in quotes.get(eid, [])[:4]:
                    ev_block.append(f"[{eid}] \"{f['quote']}\"  ({f['url']})")
            if not ev_block:
                checks.append({"sentence": sent.strip(), "cited": eids,
                               "verdict": "UNSUPPORTED",
                               "reason": "cites an evidence id that produced no "
                                         "surviving verified quote"})
                stats["verify_unsupported"] += 1
                continue
            user = (f"SENTENCE FROM THE DRAFT REPORT:\n{sent.strip()}\n\n"
                    f"THE QUOTES IT CITES:\n" + "\n".join(ev_block))
            out = model.chat_json(VERIFY_SYS, user, VERIFY_SCHEMA,
                                  temperature=0.0, num_predict=400)
            if not out:
                stats["verify_parse_fail"] += 1
                continue
            v = out.get("verdict", "UNSUPPORTED")
            checks.append({"sentence": sent.strip(), "cited": eids,
                           "verdict": v, "reason": out.get("reason", "")})
            stats[f"verify_{v.lower()}"] = stats.get(f"verify_{v.lower()}", 0) + 1
    return checks


# ---------------------------------------------------------------------- main

def build_report(question: str, plan: dict, subqs: list[dict], draft: str,
                 checks: list[dict], store: dict, findings: list[dict],
                 stats: dict, model: Model, rounds_run: int,
                 engine_health: dict, elapsed: float,
                 backends: set | None = None) -> str:
    used = sorted({f["evidence_id"] for f in findings},
                  key=lambda x: int(x[1:]))
    bad = [c for c in checks if c["verdict"] != "SUPPORTED"]
    out = [f"# {question}", ""]

    if bad:
        # Surfaced at the top, not buried in an appendix. A reader who stops after
        # the answer must still see that parts of it did not survive checking.
        out += [f"> **Verification: {len(checks) - len(bad)}/{len(checks)} cited "
                f"sentences fully supported.** {len(bad)} did not verify cleanly "
                f"and are listed under *Verification exceptions* below.", ""]
    elif checks:
        out += [f"> **Verification: all {len(checks)} cited sentences supported "
                f"by their quotes.**", ""]

    out += [draft.strip(), ""]

    if bad:
        out += ["## Verification exceptions", "",
                "Each of these is a sentence in the report above whose cited "
                "quotes did not fully establish it, judged by a separate pass "
                "that did not write them.", ""]
        for c in bad:
            out += [f"- **{c['verdict']}** — {c['sentence']}",
                    f"  - {c['reason']}"]
        out += [""]

    out += ["## Sources", ""]
    for eid in used:
        ev = store[eid]
        n = len([f for f in findings if f["evidence_id"] == eid])
        out.append(f"- **[{eid}]** [{ev.title or ev.url}]({ev.url}) — "
                   f"{n} verified finding(s), {ev.chars:,} chars")
    unused = [e for e in store.values() if e.id not in set(used)]
    if unused:
        failed = [e for e in unused if not e.ok]
        empty = [e for e in unused if e.ok]
        out += ["", f"Also retrieved but not cited: {len(empty)} page(s) "
                    f"yielded no usable finding; {len(failed)} could not be "
                    f"fetched or were too short to use."]

    out += ["", "## How this was produced", "",
            f"- Model: `{model.model}` at num_ctx {model.num_ctx:,}, "
            f"think={'on' if model.think else 'off'} — {model.calls} calls, "
            f"{model.total_s / 60:.1f} min of model time"
            + (f" ({model.truncated} truncated)" if model.truncated else ""),
            f"- {rounds_run} gather round(s); "
            f"{len(store)} page(s) retrieved, {len(used)} cited",
            f"- Quotes: {stats['quotes_kept']} verified against the fetched page, "
            f"{stats['quotes_rejected']} rejected as not literally present"
            + (f"; {stats['subq_remapped']} findings re-filed under the "
               f"sub-question they actually answered"
               if stats['subq_remapped'] else ""),
            f"- Wall clock: {elapsed / 60:.1f} min"]
    if backends:
        out += [f"- Search backends used: {', '.join(sorted(backends))}"]
    if engine_health:
        detail = "; ".join(f"{k} ({', '.join(sorted(v))})"
                           for k, v in sorted(engine_health.items()))
        out += [f"- Search backends/engines unavailable during this run: {detail} — "
                f"coverage may be narrower than it looks"]
    out += ["",
            "*Search and page retrieval were performed by the harness, not by "
            "the model: the model could not choose to skip a fetch and report "
            "otherwise. Every quote above was checked as a literal substring of "
            "the retrieved page before it was allowed into the report.*"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Two calling conventions. Interactive use passes --question/--out. The
    # ollama-queue.py --runner contract passes exactly five flags and nothing
    # else -- --model, --host, --num-ctx, --cwd, --task-file -- so the question
    # arrives as a FILE and the output directory is --cwd. Both are supported
    # rather than migrating, because the queue owns host/model assignment and
    # the VRAM guards, and a run launched by hand for debugging should not have
    # to fake a task file to get the same behaviour.
    ap.add_argument("--question", default=None)
    ap.add_argument("--task-file", default=None,
                    help="file containing the research question (queue contract)")
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--cwd", default=None,
                    help="output directory, queue contract spelling of --out")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--searxng", default=DEFAULT_SEARXNG)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--temperature", type=float, default=0.3,
                    help="synthesis only; plan/extract/gap/verify are greedy")
    ap.add_argument("--rounds", type=int, default=3,
                    help="max gather rounds (round 1 is the plan's queries)")
    ap.add_argument("--per-query", type=int, default=6,
                    help="search results taken per query")
    ap.add_argument("--max-sources", type=int, default=40,
                    help="hard cap on pages fetched across the whole run")
    ap.add_argument("--new-per-round", type=int, default=14)
    ap.add_argument("--fetch-chars", type=int, default=40000,
                    help="chars kept per fetched page in the evidence store")
    ap.add_argument("--extract-chars", type=int, default=9000,
                    help="chars of each page shown to the extraction model")
    ap.add_argument("--workers", type=int, default=8, help="parallel fetches")
    ap.add_argument("--chunks-per-source", type=int, default=2,
                    help="how many relevance-ranked sections of each page to "
                         "read (1 = first N chars only, the old behaviour)")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--tavily-budget", type=int, default=8,
                    help="max Tavily searches per run (its free tier is a capped "
                         "1,000 credits/MONTH, so this is a real budget, not a "
                         "rate limit); 0 disables Tavily entirely")
    ap.add_argument("--think", action="store_true",
                    help="allow the model's reasoning mode (OFF by default: on "
                         "qwen3.5:9b it consumed the entire token budget and "
                         "returned empty content)")
    ap.add_argument("--force", action="store_true",
                    help="load even if the VRAM preflight says it will not fit")
    args = ap.parse_args()

    if args.task_file and not args.question:
        try:
            args.question = Path(args.task_file).expanduser().read_text().strip()
        except Exception as e:
            print(f"ERROR: could not read --task-file {args.task_file}: {e}",
                  file=sys.stderr)
            return 2
    if not args.question:
        print("ERROR: need --question or --task-file", file=sys.stderr)
        return 2
    out = args.out or args.cwd
    if not out:
        print("ERROR: need --out or --cwd", file=sys.stderr)
        return 2
    _mis = (not _dbk.is_darkbloom(args.host)) and _dbk.legacy_ollama_misroute(args.host)
    if _mis:       # W2: never run a research job down the Ollama protocol while Darkbloom is the backend
        print(f"ERROR: {_mis}", file=sys.stderr)
        return 2

    outdir = Path(out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    model = Model(args.host, args.model, args.num_ctx, args.temperature,
                  think=args.think)
    _RESEARCH_META.update({"model": args.model, "question": args.question})
    atexit.register(_emit_research_metrics)
    stats = {"quotes_kept": 0, "quotes_rejected": 0, "extract_parse_fail": 0,
             "bad_subq_id": 0, "subq_remapped": 0, "verify_parse_fail": 0,
             "verify_unsupported": 0}
    store: dict[str, Evidence] = {}
    seen_urls: set = set()
    seen_queries: set = set()
    engine_health: dict = {}
    backends_seen: set = set()
    # Tavily's free tier is 1,000 credits/MONTH. Capping per run keeps one
    # question from eating a meaningful share of it when ollama.com is down.
    tavily_budget = {"tavily": args.tavily_budget}
    findings: list[dict] = []

    log(f"question: {args.question}")
    log(f"model: {args.model} @ {args.host}  ctx={args.num_ctx}")

    if not preflight_vram(args.host, args.model, args.num_ctx, args.force):
        return 2

    # -- preflight: a research run on a dead search backend produces a confident
    # -- empty report, which is the worst possible output. Fail before spending
    # -- an hour of GPU discovering it.
    live = searxng_search(args.searxng, args.question[:80], GENERAL_ENGINES, 5)
    if not live[0]:
        log(f"ABORT: SearXNG at {args.searxng} returned nothing for the question "
            f"itself. Unresponsive: {live[1]}")
        return 3

    # ---- stage 1: plan
    log("stage 1/6: plan")
    plan = model.chat_json(PLAN_SYS, f"RESEARCH QUESTION: {args.question}",
                           PLAN_SCHEMA, temperature=0.0, num_predict=2500)
    def _usable(pl: dict) -> list:
        return [q for q in (pl.get("sub_questions") or [])
                if q.get("question") and looks_like_a_question(q["question"])]

    if not plan or not _usable(plan):
        # One retry, then carry on with the question as its own sub-question.
        # Aborting here throws away an entire run over one flaky JSON object,
        # and the un-decomposed question is still a researchable question --
        # a shallower run beats no run.
        bad = [q.get("question", "") for q in (plan or {}).get("sub_questions", [])]
        log(f"  planner returned no usable sub-questions -- retrying once"
            + (f" (got search-query-shaped strings: {bad[:2]})" if bad else ""))
        # The retry samples deliberately: the greedy path just failed, so
        # repeating it verbatim would fail identically.
        plan = model.chat_json(PLAN_SYS, f"RESEARCH QUESTION: {args.question}",
                               PLAN_SCHEMA, temperature=0.5,
                               num_predict=2500) or {}
        if not _usable(plan):
            log("  planner failed twice -- falling back to the question itself "
                "as a single sub-question (coverage will be shallower)")
            plan = {"sub_questions": [{"id": "Q1", "question": args.question}],
                    "queries": []}
    # Renumber canonically and rewrite the plan's queries to match. Observed
    # live: the planner returned ids "NVIDIA", "ASUS", "MSI" -- one per vendor --
    # and every downstream stage keys findings off these ids. Trusting model-chosen
    # ids means a collision or a rename silently drops findings on the floor.
    _idmap = {}
    subqs = []
    for sq in plan["sub_questions"]:
        if not sq.get("question") or not looks_like_a_question(sq["question"]):
            continue
        qid = f"Q{len(subqs)+1}"
        if sq.get("id"):
            _idmap[str(sq["id"])] = qid
        subqs.append({"id": qid, "question": sq["question"]})
    for q in (plan.get("queries") or []):
        q["sub_question_id"] = _idmap.get(str(q.get("sub_question_id")), "")
    for s in subqs:
        log(f"  {s['id']}: {s['question']}")
    queries = [q for q in (plan.get("queries") or []) if (q.get("query") or "").strip()]
    fallback = queries_from_subquestions(subqs, queries, args.question)
    if fallback:
        log(f"  planner left {len(fallback)} sub-question(s) with no query "
            f"-- deriving queries from the sub-question text")
        queries += fallback
    log(f"  {len(queries)} seed queries")

    # ---- stages 2-4: gather / extract / gap, repeated
    rounds_run = 0
    for rnd in range(1, args.rounds + 1):
        if not queries:
            log(f"round {rnd}: no queries left -- gathering complete")
            break
        room = args.max_sources - len(store)
        if room <= 0:
            log(f"round {rnd}: source cap ({args.max_sources}) reached")
            break
        rounds_run = rnd
        log(f"stage 2/6: gather (round {rnd}, {len(queries)} queries)")
        new = stage_gather(args.searxng, queries, store, seen_urls, seen_queries,
                           args.per_query, min(args.new_per_round, room),
                           args.fetch_chars, outdir, args.workers, engine_health,
                           args.question, tavily_budget)
        backends_seen |= getattr(stage_gather, "last_backends", set())
        if not new:
            log(f"round {rnd}: no new usable sources")
            if rnd == 1:
                log("ABORT: first gather round produced zero usable sources")
                return 5
            break
        log(f"stage 3/6: extract ({len(new)} sources)")
        findings += stage_extract(model, new, subqs, args.extract_chars, stats,
                                  args.chunks_per_source)
        log(f"  findings so far: {len(findings)} "
            f"(rejected {stats['quotes_rejected']} unverifiable quotes, "
            f"remapped {stats['subq_remapped']} ids, "
            f"{stats['extract_parse_fail']} parse failures)")

        if rnd >= args.rounds:
            break
        log("stage 4/6: gap check")
        gap = stage_gap(model, args.question, subqs, findings, store)
        if not gap:
            log("  gap check unparseable -- stopping gather")
            break
        unanswered = [a for a in gap.get("assessment", []) if not a.get("answered")]
        for a in unanswered:
            log(f"  still open {a.get('sub_question_id')}: "
                f"{a.get('what_is_missing','')[:110]}")
        queries = [q for q in (gap.get("queries") or [])
                   if (q.get("query") or "").strip()]
        if not queries and unanswered:
            # It named open sub-questions and then gave nothing to search for.
            # Trust the assessment over the empty list and search anyway.
            open_subqs = [s for s in subqs
                          if s["id"] in {a.get("sub_question_id") for a in unanswered}]
            queries = queries_from_subquestions(open_subqs, [], args.question)
            log(f"  gap check reported {len(unanswered)} open sub-question(s) but "
                f"no queries -- derived {len(queries)} from their text")
        # The gap check's verdict is advisory; corroboration is structural.
        # Observed live: with 12 findings drawn from only 3 usable sources, the
        # gap check declared everything settled and the run stopped after one
        # round. A model asked "is this enough?" answers about whether the
        # findings look coherent, which they can while resting on a single
        # source. Whether two independent sources exist for each sub-question is
        # a fact about the evidence store, not a judgement, so the harness
        # decides it.
        thin = []
        for sq in subqs:
            srcs = {f["evidence_id"] for f in findings
                    if f["sub_question_id"] == sq["id"]}
            if len(srcs) < 2:
                thin.append(sq)
        if not queries and thin:
            queries = queries_from_subquestions(thin, [], args.question)
            log(f"  gap check said settled, but {len(thin)} sub-question(s) rest "
                f"on fewer than 2 independent sources -- searching anyway")
        if not queries:
            log("  gap check reports everything settled")
            break

    if not findings:
        # Not a failure. On a question whose premise is false, this IS the
        # answer, and it is the answer weaker research produces least often --
        # the failure mode for "which lab published X and at what conference"
        # is a confident invented lab and venue, not an empty response. A run
        # that searched N queries, retrieved M real pages and found nothing
        # supporting the premise has established something worth stating
        # precisely, so state it precisely rather than emitting a stub.
        log(f"no verified findings from {len(store)} retrieved page(s) "
            f"-- writing a negative-result report")
        fetched_ok = [e for e in store.values() if e.ok]
        lines = [f"# {args.question}", "",
                 "**Nothing found to support the premise of this question.**", "",
                 f"{len(seen_queries)} search queries were run and "
                 f"{len(fetched_ok)} pages were retrieved and read in full. Not "
                 f"one contained a statement bearing on the question that could "
                 f"be quoted and verified against the page it came from.", "",
                 "Read this as evidence about the question, not as a broken run: "
                 "if the thing asked about existed and were documented anywhere "
                 "these searches reach, some page among these would have said so. "
                 "The likeliest readings are that the premise is mistaken, that "
                 "the subject is known by a different name, or that it is not "
                 "documented on the open web.", ""]
        if stats["quotes_rejected"]:
            lines += [f"{stats['quotes_rejected']} quote(s) WERE proposed during "
                      f"extraction and rejected as not literally present in the "
                      f"page they were attributed to. That is the harness "
                      f"catching invention, and it is worth knowing that the "
                      f"model reached for it here.", ""]
        lines += ["## Sub-questions, none of which were settled", ""]
        lines += [f"- **{q['id']}** {q['question']}" for q in subqs]
        lines += ["", "## Queries run", ""]
        lines += [f"- `{q}`" for q in sorted(seen_queries)]
        lines += ["", "## Pages retrieved and read", ""]
        lines += [f"- [{e.title or e.url}]({e.url}) — {e.chars:,} chars"
                  for e in fetched_ok]
        failed = [e for e in store.values() if not e.ok]
        if failed:
            lines += ["", f"A further {len(failed)} page(s) could not be "
                          f"retrieved (403, JS shell, or timeout) and were not read."]
        if engine_health:
            detail = "; ".join(f"{k} ({', '.join(sorted(v))})"
                               for k, v in sorted(engine_health.items()))
            lines += ["", f"**Search engines unavailable during this run:** "
                          f"{detail}. A negative result is weaker evidence when "
                          f"the search was narrow -- weigh it accordingly."]
        (outdir / "report.md").write_text("\n".join(lines) + "\n")
        (outdir / "run.json").write_text(json.dumps({
            "question": args.question, "model": args.model,
            "sub_questions": subqs, "findings": [], "checks": [], "stats": stats,
            "rounds": rounds_run, "queries": sorted(seen_queries),
            "engine_health": {k: sorted(v) for k, v in engine_health.items()},
            "sources": [e.to_dict() for e in store.values()],
            "model_calls": model.calls, "elapsed_s": round(time.time() - t0, 1),
            "negative_result": True,
        }, indent=2))
        return 0

    # ---- stage 5: synthesis
    log(f"stage 5/6: synthesis ({len(findings)} findings, "
        f"{len({f['evidence_id'] for f in findings})} sources)")
    draft = stage_synth(model, args.question, subqs, findings, store)

    # ---- stage 6: verification
    checks: list[dict] = []
    if not args.no_verify:
        log("stage 6/6: verification")
        checks = stage_verify(model, draft, findings, stats)
        ok = len([c for c in checks if c["verdict"] == "SUPPORTED"])
        log(f"  {ok}/{len(checks)} cited sentences fully supported")

    elapsed = time.time() - t0
    report = build_report(args.question, plan, subqs, draft, checks, store,
                          findings, stats, model, rounds_run, engine_health,
                          elapsed, backends_seen)
    (outdir / "report.md").write_text(report)
    (outdir / "run.json").write_text(json.dumps({
        "question": args.question, "model": args.model, "num_ctx": args.num_ctx,
        "plan": plan, "sub_questions": subqs, "findings": findings,
        "checks": checks, "stats": stats, "rounds": rounds_run,
        "engine_health": {k: sorted(v) for k, v in engine_health.items()},
        "sources": [e.to_dict() for e in store.values()],
        "model_calls": model.calls, "model_seconds": round(model.total_s, 1),
        "elapsed_s": round(elapsed, 1),
    }, indent=2))
    evdir = outdir / "evidence"
    evdir.mkdir(exist_ok=True)
    for e in store.values():
        if e.ok:
            (evdir / f"{e.id}.txt").write_text(f"{e.url}\n{e.title}\n\n{e.text}")
    try:
        RESIDENCY_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    log(f"done in {elapsed/60:.1f} min -> {outdir/'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
