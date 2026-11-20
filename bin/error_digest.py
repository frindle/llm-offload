#!/usr/bin/env python3
"""error_digest: one plain-language digest per day of the errors the Unraid docker containers logged.

The operator's OK, 2026-11-15: "automated error handling for docker containers essentially" (scoped to the
Unraid containers). Runs on Unraid (queue container), reads the operator's Loki log stack, never changes a container: no restarts,
no fix jobs (not approved; ask first).

  error_digest.py run [--day YYYY-MM-DD] [--model M | --no-model] [--no-send]
                                                                     build one day's digest (default: yesterday)
  error_digest.py backfill --days N [--no-model]                     build the last N days, oldest first
  error_digest.py show [--day YYYY-MM-DD]                            print a digest
  error_digest.py days                                               the last 14 digests with their counts
  error_digest.py tick                                               the container's daily run (every.sh 300): see
                                                                     cmd_tick
On a machine whose queue runs elsewhere (queue_config.queue_is_remote(), e.g. the operator's Mac), show and days read
the queue API's GET /api/error-digest (the queue token; see api_read) instead of the local digest dir.
  error_digest.py grading --days N                                   one page for the operator to mark the errors worth knowing

Per day it writes <dir>/digests/<day>.json and <day>.md (dir: $ERROR_DIGEST_DIR, else <dispatch dir>/error-digest).
The plain digest is always written: per container, error lines grouped into kinds (a "signature": the line with times,
numbers, ids, addresses, URLs and secrets taken out), counts, and which kinds are NEW (not seen in the 14 days before).
Known noise (error_digest_noise.json, plus noise.json in the digest dir) is folded into "routine" counts or left out.
Alert rules (error_digest_alerts.json, plus alerts.json in the digest dir) route kinds "for you" (the daily message)
or "for Claude" (logged for the operator's Claude sessions), or hold them back; see "alert routing" below. The daily
message is <day>.notify.txt; when $ERROR_DIGEST_NOTIFY_URL and its token are set, `run` (yesterday only, once per
day, never backfill) also POSTs it there; `tick` sends it again when a stop or an outage cut that off. Unset: shadow, the file only. The local model only adds one
"what it probably means" line per new kind: `run` enqueues one queue job (error-digest-runner.py, Unraid lane) that
fills them in later; when that job fails the digest stays as plain counts.
A "Plex and Glacier" section is added from plex_glacier_watch.py's samples for that day, when there are any, and a
"Playback" section from plex_playback.py (real stalls per Plex client, skips and starts left out, with evidence and a
one-line pointer per buffering spell); spells also go into the daily message.

Loki: $LOKI_URL (or $LOGQ_URL, or queue config logs.loki_url) and a bearer token from $LOKI_TOKEN, the file
$LOKI_TOKEN_FILE / $LOGQ_TOKEN_FILE, or the macOS Keychain item service loki-token account loki. The token is never
printed. Loki already redacts tokens, passwords, API keys and Authorization headers before storing; signatures redact
again (key=value secrets, emails, URLs with their query strings, IPs).
"""
import contextlib
import datetime as dt
import hashlib
import html
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

LOOKBACK_DAYS = 14          # a kind is NEW when none of the 14 days before had it
PAGE = 5000                 # Loki's default max entries per query
LINE_CAP = 60000            # per container per day; past it the counts are a floor ("capped")
SIG_LEN = 200
MODEL_DEFAULT = "qwen3:8b"
MODEL_SIGS_PER_CONTAINER = 8

# LogQL line filter (RE2): the words that make a line "error-ish". Python re below classifies them.
# The second half catches an exception's own line, "ConnectionResetError: [Errno 104] Connection reset by peer", which
# carries no error word on its own (the class name ends in Error/Exception).
LOGQL_ERR = (r"(?i)\b(error|errors|exception|traceback|fatal|critical|panic|failed|failure|warn|warning)\b"
             r"|(?-i:[a-z](?:Error|Exception)\b)")
ERR_WORDS = re.compile(r"\b(error|errors|exception|traceback|fatal|critical|panic|failed|failure)\b", re.I)
ERR_CLASS = re.compile(r"[a-z](?:Error|Exception)\b")
WARN_WORDS = re.compile(r"\b(warn|warning)\b", re.I)
# phrases that carry an error word but report that nothing went wrong
BENIGN = re.compile(
    r"\b(?:0|no|zero|without)\s+(?:errors?|failures?|exceptions?)\b"
    r"|\b(?:errors?|failures?|failed|exceptions?|warnings?)(?:_count|_total|_rate)?[\"']?\s*[=:]\s*[\"']?"
    r"(?:0(?:\.0+)?|none|null|false|\[\]|\{\})(?![\w.])"
    r"|\berror_?(?:log|page|handler|reporting|level)\b"
    r"|\bon_?error\b|\bignore_?errors?\b|\bfail_?(?:fast|over|safe)\b|\bnon-?critical\b"
    r"|\bhad_?errors?[\"']?\s*[=:]\s*[\"']?false\b"            # node's "Socket closed, hadError=false"
    r"|\bcritical-exit\b|\bwatching for fatal\b",              # a supervisord listener's name and its start line
    re.I)


# ---------------------------------------------------------------------------------------------------- normalizing
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SUBS = [
    (re.compile(r"(?i)\b(authorization|bearer|token|access_token|refresh_token|api[_-]?key|apikey|secret|password|"
                r"passwd|pwd|cookie|session(?:id)?|sig(?:nature)?|auth)\b(\"?\s*[:=]\s*\"?|\s+)[^\s\"',;&]+"),
     r"\1\2<redacted>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>"),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>)]+(?:<redacted>[^\s\"'<>)]*)*"), lambda m: _url_shape(m.group(0))),
    (re.compile(r"\b(\w+_id|\w+_ID|[a-z]+Id|id|ID|Id)(\"?\s*[=:]\s*\"?)[A-Za-z0-9._:-]{3,}"), r"\1\2<id>"),
    (re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?)?"), "<time>"),
    (re.compile(r"\b\d\d:\d\d:\d\d(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"(?i)\b(?:mon|tue|wed|thu|fri|sat|sun)?,?\s*\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                r"[a-z]*\s+\d{4}\b"), "<date>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b"), "<mac>"),
    (re.compile(r"(?i)\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"(?i)\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,11}\b"), "<hex>"),   # short hashes ('8a3f1b2c')
    (re.compile(r"(?i)\b0x[0-9a-f]+\b"), "<hex>"),
    (re.compile(r"(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])[A-Za-z0-9+/_-]{32,}={0,2}"), "<blob>"),
    (re.compile(r"\d+(?:\.\d+)*"), "N"),
]
_QUOTED = re.compile(r"(?<!\w)'(?:[^'\n]|'(?=\w)){1,160}'(?!\w)")     # 'a name', 'A Title (N)', 'Grey's'
_URL_PATH = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s?\"'<>)]+)[^\s\"'<>)]+")


def _url_shape(u):
    """A URL keeps its scheme and host shape, never its path ids or query string (tokens hide there)."""
    try:
        p = urllib.parse.urlsplit(u)
        host = p.hostname or ""
        if re.fullmatch(r"[\d.]+|[0-9a-f:]+", host or "x", re.I):
            host = "<ip>"
        path = "/".join("N" if re.search(r"\d", seg) else seg for seg in (p.path or "").split("/")[:3])
        return "%s://%s%s%s" % (p.scheme, host, path, "?…" if p.query else "")
    except ValueError:
        return "<url>"


def clean(line):
    """The raw line without ANSI colour codes or control characters (still the real text: only used locally)."""
    s = _ANSI.sub("", line or "")
    return "".join(ch if ch >= " " or ch == "\t" else " " for ch in s).strip()


_COUNT_KEY = re.compile(r"(?i)(?:errors?|failures?|failed)(?:_count|_total)?")


def _err_counts(d, path=(), depth=0):
    """Nonzero error counts nested in a JSON line ({"llm": {"errors": 1}} -> ["llm errors 1"]): the only part of a
    line like '{"event": "result_posted", ...}' that says why it is an error (11-15: the model invented a reason)."""
    out = []
    for k, v in d.items():
        if isinstance(v, dict) and depth < 3:
            out += _err_counts(v, path + (str(k),), depth + 1)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v and _COUNT_KEY.fullmatch(str(k)):
            out.append(" ".join(path[-1:] + (str(k), str(v))))
    return out[:3]


def _json_line(s):
    """A JSON log line as 'level logger: msg (error)', the fields a person reads; None when it isn't one."""
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    pick = lambda *ks: next((str(d[k]) for k in ks if isinstance(d.get(k), (str, int, float)) and str(d[k])), "")
    msg = pick("msg", "message", "event", "error_message")
    if not msg:
        return None
    err = pick("error", "err", "exception", "reason") or ", ".join(_err_counts(d))
    lv, lg = pick("level", "lvl", "severity"), pick("logger", "name", "module", "component")
    return " ".join(x for x in (lv, (lg + ":") if lg else "", msg, ("(%s)" % err) if err else "") if x)


def shape(line):
    """The redacted line: times, numbers, ids, addresses, URLs, emails and secrets taken out; at most 200 chars."""
    s = clean(line)
    s = _json_line(s) or s
    for rx, rep in _SUBS:
        s = rx.sub(rep, s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bN(?:[\s,:/.\-]+N\b)+", "N", s)            # runs of numbers -> one N
    s = re.sub(r"<time>(?:[\s,]*<time>)+", "<time>", s)
    s = re.sub(r"^(?:(?:time|ts|t)=)?<time>\s*", "", s.strip())          # a leading timestamp says nothing
    return s.strip()[:SIG_LEN]


def signature(line):
    """The kind of an error line: its shape() with single-quoted names folded ('libfoo.so', 'A Show SN' -> '<q>') and
    URLs cut to their host, so two lines that differ only in a number, id, address, title or request path are one
    kind (the digest still shows the first line's own shape)."""
    return _URL_PATH.sub(r"\1/…", _QUOTED.sub("'<q>'", shape(line)))


_MAIL_URL = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://(?:<\w+>|[^/\s?\"'<>)])+)[^\s\"'<>)]+")   # an <ip> host too
_DQUOTED = re.compile(r'"(?:[^"\\\n]|\\.){1,160}"')                                   # "a \"name\"" as one
_DQUOTED_PLAIN = re.compile(r'"[^"\n]{1,160}"')                                       # "C:\dir\" "a name": two
# to the end of the line (a folder name can hold spaces): /abs (also after ':', '=', '(' or '.'), ./rel, ../rel, ~/x,
# C:\x, \\server\share, //server/share, file:///x, and a relative path of 2+ segments (media/Folder/x); never the // of
# a URL or a URL's path already cut to /…
_PATH = re.compile(r"(?:(?<![\w/~])(?:\.\.?/|~?/(?![/…]))|\b[A-Za-z]:\\|\\\\[\w.$-]+\\|(?<![\w:/~])//[\w.$-]+/"
                   r"|(?i:\bfile:/+)|(?<![\w/~.:<>-])[A-Za-z][\w.-]+/[^\s/]+/)\S.*$")
_DOTTED = re.compile(r"\b\w[\w'()\[\]+-]*(?:\.[\w'()\[\]+-]+){3,}")          # Some.Show.SNEN.Np.WEB-GRP


def mail_sample(sample):
    """A sample line as the daily message may carry it (the message goes through the mail Worker): signature()'s
    folding (single-quoted names, URL paths, here also after an <ip> host), plus double-quoted names (escaped quotes
    inside), file paths (from the path to the end of the line) and dotted release names taken out. A backstop for the
    shapes it can see, not a filter: an unquoted name in plain words still gets through, which is why an owner rule
    whose lines name a download or file sets "names" (no example line is mailed). The digest on Unraid keeps the full
    shape()."""
    s = _MAIL_URL.sub(r"\1/…", _QUOTED.sub("'<q>'", sample or ""))
    s = _fold_dquoted(s)
    s = _PATH.sub("<path>", s)
    return _DOTTED.sub("<name>", s)


def _fold_dquoted(s):
    """Double-quoted strings out, reading a backslash before a quote both ways: as an escape ("a \"name\"") and as
    a plain character ("C:\\dir\\" then "a name"). What either reading sees as quoted is folded (spans merged)."""
    spans = sorted(m.span() for rx in (_DQUOTED, _DQUOTED_PLAIN) for m in rx.finditer(s))
    out, last, cur = [], 0, None
    for a, b in spans:
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
            continue
        if cur:
            out += [s[last:cur[0]], '"<q>"']
            last = cur[1]
        cur = [a, b]
    if cur:
        out += [s[last:cur[0]], '"<q>"']
        last = cur[1]
    return "".join(out) + s[last:]


def sig_id(container, sig):
    return hashlib.sha1((container + "\0" + sig).encode()).hexdigest()[:10]


_LV = r"(trace|trc|debug|dbg|info|inf|notice|warn|warning|wrn|err|error|eror|fatal|crit|critical|panic|alert|emerg)"
_TAG_BRACKET = re.compile(r"[\[<(]" + _LV + r"[\]>)]", re.I)
_TAG_KV = re.compile(r"\b(?:level|lvl|severity|loglevel)[\"']?\s*[:=]\s*[\"']?" + _LV + r"\b", re.I)
_TAG_LEAD = re.compile(r"^[\d\-/:.,TZ+ ]*(?:\[[\w.\-]+\]\s*)*(?:[A-Za-z_][\w.\-]*:?\s+)?(TRACE|TRC|DEBUG|DBG|INFO|INF|NOTICE|WARN|"
                       r"WARNING|WRN|ERROR|ERR|FATAL|CRIT|CRITICAL|PANIC)\b")       # INF/WRN/ERR: zerolog (cloudflared)
# ("[queue] WARNING: ..." and "[entrypoint] WARNING: ...": a bracketed component name may come before the level)
_TAG_DASH = re.compile(r"\s[-|]\s(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s[-|:]")
_LV_MAP = {"trace": None, "trc": None, "debug": None, "dbg": None, "info": None, "inf": None, "notice": None,
           "warn": "warn", "warning": "warn", "wrn": "warn"}


def level_tag(s):
    """The level the line's own logger gave it ('error' | 'warn' | None for info/debug), or 'untagged'."""
    head = s[:100]
    for rx, text in ((_TAG_KV, s), (_TAG_BRACKET, head), (_TAG_LEAD, head), (_TAG_DASH, head)):
        m = rx.search(text)
        if m:
            return _LV_MAP.get(m.group(1).lower(), "error")
    return "untagged"


def level(line):
    """'error' | 'warn' | None. The logger's own level tag wins ([Warn] ... Error -> warn; INFO ... failed -> None);
    an untagged line is an error/warning by its words, except a 'nothing went wrong' phrase like '0 errors'."""
    s = clean(line)
    t = level_tag(s)
    if t != "untagged":
        return t
    s = BENIGN.sub(" ", s)
    if ERR_WORDS.search(s) or ERR_CLASS.search(s):
        return "error"
    if WARN_WORDS.search(s):
        return "warn"
    return None


# ---------------------------------------------------------------------------------------------------- fragments
# Lines that belong to a longer error but say nothing on their own: traceback headers, stack frames, a source line
# echoed in a Python traceback, HTML error-page markup. They are counted as "fragments", never made into kinds: the
# line that carries the message ("ConnectionResetError: ... reset by peer", "System.Net.WebException: ...") is the kind.
_GLUE = re.compile(r"-{8,}")       # Python's socketserver prints 40 dashes around a traceback; threads glue them on
_SOCKSERVER = re.compile(r"^Exception (?:occurred|happened) during processing of request from\s*(?:\([^)]*\))?\s*")
_HTML = ("html|head|body|title|meta|link|script|style|div|span|p|a|br|hr|h[1-6]|ul|ol|li|table|tbody|thead|tr|td|th|"
         "section|header|footer|nav|main|article|img|button|form|input|noscript|center|b|i|u|strong|em|small|pre|code|"
         "svg|path|iframe|label|sup|sub|font")
_FRAGMENT = re.compile(
    r"^(?:C\+\+ )?Traceback \(most recent call last\):?$"
    r"|^File \"[^\"]+\", line \d+(?:, in \S+)?$"
    r"|^at (?:async |new )?\S*[./]\S*?(?:\s*\(.*\))?(?:\s+in\s+\S.*)?$"                  # .NET / Java / JS stack frame
    r"|^-+ ?End of (?:inner exception )?stack trace(?: from previous location)?(?: where exception was thrown)? ?-+$"
    r"|^\.\.\. \d+ (?:more|common frames omitted)$"
    r"|^Error Message Summary:?$"
    r"|^<(?:!doctype|!--|/?(?:" + _HTML + r")\b)",                             # HTML error-page markup
    re.I)
# a Python source line echoed under a traceback frame: a dotted call or an assignment from a call, and nothing after
# it ("self._err(ShimError(500, 'internal error'))", "warnings.warn(msg)"); never "db.connect(): failed (timeout)"
_CODE = re.compile(r"^(?!.*\)\s*:\s)(?:raise |return |await )?"
                   r"(?:[a-z_]\w*(?:\.\w+)+|[\w.\[\]\"']+\s*=\s*[\w.]+)\(.*\)\s*(?:#.*)?$")


def unglue(line):
    """The cleaned line without socketserver's dash rules and its 'request from (addr)' header (the address of
    whoever connected says nothing about the error; what follows the header, if anything, is the line)."""
    s = _GLUE.sub(" ", line)
    s = _SOCKSERVER.sub("", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def is_fragment(line):
    """True for a stack frame, traceback header, echoed source line or page markup (see above), or an empty line."""
    s = line.strip()
    return not s or bool(_FRAGMENT.match(s) or _CODE.match(s))


# ---------------------------------------------------------------------------------------------------- noise rules
def load_noise(path):
    """noise.json: [{"container": "<glob>", "match": "<regex on the line>", "action": "routine"|"skip", "why": "..."}].
    routine: counted as routine (never NEW, no model line); skip: left out (alerted elsewhere). Bad rules are dropped."""
    import fnmatch
    try:
        rules = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for r in rules if isinstance(rules, list) else []:
        try:
            if r.get("action") in ("routine", "skip") and r.get("match"):
                out.append((r.get("container") or "*", re.compile(r["match"], re.I), r["action"], fnmatch))
        except (re.error, AttributeError, TypeError):
            continue
    return out


def noise_action(rules, container, line):
    for glob, rx, action, fnm in rules:
        if fnm.fnmatchcase(container, glob) and rx.search(line):
            return action
    return None


# ---------------------------------------------------------------------------------------------------- alert routing
# Who should hear about a kind of error: "owner" (the operator: in the daily message) or "claude" (the operator's Claude
# sessions read and handle them; never in the message). Rules: error_digest_alerts.json, plus <digest dir>/alerts.json
# checked first. A line goes to the first rule that matches it. Rule fields:
#   route owner|claude, container "<glob>" or [globs], match "<regex on the line>", what "<one line for the message>",
#   min_count N (default 1: fewer lines that day and it is held back), sustain_min M + gap_min G (lines at most G
#   minutes apart are one run; held back unless a run lasts M minutes; a run that began before midnight is measured
#   whole on the day it ends), hold "<why>" (held back whatever the count), names true (its lines name a download or
#   a file: the daily message leaves the example line out; the digest on Unraid keeps it), counted_only true (only
#   lines the digest counts as errors or warnings, see level(): for a catch-all rule, so an INFO line that merely
#   mentions "error", such as supervisord naming the error-digest program, does not match).
# do "<one line>" (owner rules: what the operator should do, shown under the item in the daily message).
# Held-back items are listed in the digest and named with their reason in the message, without an example line. New kinds
# of error that no rule covers go to Claude, never straight to the operator ("for Claude": the operator's Claude
# sessions read the digest and handle or ask; nothing here restarts or fixes anything).
NEW_FOR_CLAUDE = 15        # new unrouted kinds listed for Claude per day; the rest are counted


def load_alerts(*paths):
    import fnmatch  # noqa: F401 - match_alerts uses it; imported here so a bad rules file fails the same way
    out, seen = [], set()
    for path in paths:
        try:
            rules = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        for r in rules if isinstance(rules, list) else []:
            try:
                if r.get("route") not in ("owner", "claude") or not r.get("match") or not r.get("what"):
                    continue
                globs = r.get("container") or "*"
                globs = [globs] if isinstance(globs, str) else [str(g) for g in globs]
                key = (tuple(globs), r["match"], r["route"])
                if key in seen:
                    continue
                a = {"globs": globs, "rx": re.compile(r["match"], re.I), "route": r["route"], "what": str(r["what"]),
                     "min_count": max(1, int(r.get("min_count", 1))), "sustain_min": float(r.get("sustain_min", 0)),
                     "gap_min": float(r.get("gap_min", 10)), "hold": str(r.get("hold") or ""),
                     "names": r.get("names") is True, "counted_only": r.get("counted_only") is True,
                     "do": str(r.get("do") or "")[:200]}
                seen.add(key)
                out.append(a)
            except (re.error, AttributeError, TypeError, ValueError):
                continue
    return out


def match_alerts(container, rows, arules):
    """{rule index: {"ts": [ns], "sample": shape of the first line, "sigs": {sig ids}}} for this container's lines."""
    import fnmatch
    mine = [(i, a) for i, a in enumerate(arules) if any(fnmatch.fnmatchcase(container, g) for g in a["globs"])]
    hits = {}
    if not mine:
        return hits
    for ns, raw in rows:
        line = unglue(clean(raw))
        lv = level(line) if any(a["counted_only"] for _, a in mine) else None
        for i, a in mine:
            if a["counted_only"] and lv is None:
                continue
            if a["rx"].search(line):
                h = hits.get(i)
                if h is None:
                    h = hits[i] = {"ts": [], "sample": shape(line), "sigs": set()}
                h["ts"].append(ns)
                if len(h["sigs"]) < 200:
                    h["sigs"].add(sig_id(container, signature(line)))
                break
    return hits


def runs(ts, gap_ns):
    """[(first_ns, last_ns, lines)] of sorted times, a new run after a gap longer than gap_ns."""
    out = []
    for t in ts:
        if out and t - out[-1][1] <= gap_ns:
            out[-1] = (out[-1][0], t, out[-1][2] + 1)
        else:
            out.append((t, t, 1))
    return out


def decide_alerts(acc, arules, containers, before=None):
    """acc {rule index: [(container, hits)]} -> rec["alerts"]: owner, claude, held items and new kinds for Claude.
    before {rule index: [ns]}: the rule's lines just before midnight, so a run that began the day before is measured
    whole (only runs that reach into this day count)."""
    out = {"owner": [], "claude": [], "held": [], "new_for_claude": [], "new_for_claude_more": 0}
    covered = set()
    for i in sorted(acc):
        a, per = arules[i], acc[i]
        ts = sorted(t for _, h in per for t in h["ts"])
        for _, h in per:
            covered |= h["sigs"]
        item = {"what": a["what"], "route": a["route"], "containers": sorted(c for c, _ in per), "count": len(ts),
                "first_ns": ts[0], "last_ns": ts[-1], "sample": _short(per[0][1]["sample"], 200)}
        if a.get("names"):
            item["names"] = True
        if a.get("do"):
            item["do"] = a["do"]
        held = []
        if len(ts) < a["min_count"]:
            held.append("%d× today, %d needed" % (len(ts), a["min_count"]))
        if a["sustain_min"]:
            pre = sorted(t for t in (before or {}).get(i, []) if t < ts[0])
            rr = [r for r in runs(pre + ts, a["gap_min"] * 60e9) if r[1] >= ts[0]]
            longest = max(r[1] - r[0] for r in rr) / 60e9
            item["longest_min"] = round(longest, 1)
            item["runs"] = [list(r) for r in sorted(rr, key=lambda r: r[0] - r[1])[:3]]
            if longest < a["sustain_min"]:
                held.append("longest run %d min, %d needed" % (longest, a["sustain_min"]))
        if a["hold"]:
            held.append(a["hold"])
        if held:
            item["held"] = "; ".join(held)
            out["held"].append(item)
        else:
            out[a["route"]].append(item)
    new = [(c, s) for c, i in containers.items() for s in i["sigs"]
           if s.get("new") and s["level"] == "error" and s["id"] not in covered]
    new.sort(key=lambda x: -x[1]["count"])
    out["new_for_claude"] = [{"container": c, "id": s["id"], "count": s["count"], "text": _short(s["text"], 200)}
                             for c, s in new[:NEW_FOR_CLAUDE]]
    out["new_for_claude_more"] = max(0, len(new) - NEW_FOR_CLAUDE)
    return out


# ---------------------------------------------------------------------------------------------------- Loki
class LokiError(Exception):
    pass


def _keychain_token():
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-w", "-a", "loki", "-s", "loki-token"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def loki_settings():
    url = (os.environ.get("LOKI_URL") or os.environ.get("LOGQ_URL") or "").strip()
    if not url:
        try:
            import queue_config
            url = queue_config.get_str("logs.loki_url", "")
        except Exception:
            url = ""
    tok = os.environ.get("LOKI_TOKEN", "").strip()
    if not tok:
        for k in ("LOKI_TOKEN_FILE", "LOGQ_TOKEN_FILE"):
            p = os.environ.get(k)
            if p:
                try:
                    tok = Path(p).read_text().strip()
                except OSError:
                    pass
                if tok:
                    break
    if not tok and sys.platform == "darwin":
        tok = _keychain_token()
    return url.rstrip("/"), tok


class Loki:
    def __init__(self, url, token, timeout=120):
        if not url:
            raise LokiError("no Loki URL: set LOKI_URL (or queue config logs.loki_url)")
        self.url, self.token, self.timeout = url, token, timeout

    def get(self, path, **params):
        req = urllib.request.Request(self.url + path + "?" + urllib.parse.urlencode(params),
                                     headers={"Authorization": "Bearer " + self.token} if self.token else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LokiError("Loki answered %s for %s" % (e.code, path)) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LokiError("Loki unreachable (%s)" % type(e).__name__) from None

    def error_counts(self, start, end):
        """{container: error-ish line count} for [start, end) (unix seconds)."""
        q = 'sum by (container) (count_over_time({job="docker"} |~ "%s" [%ds]))' % (_esc(LOGQL_ERR), int(end - start))
        d = self.get("/loki/api/v1/query", query=q, time=str(int(end * 1e9) - 1))
        out = {}
        for r in (d.get("data") or {}).get("result") or []:
            c = (r.get("metric") or {}).get("container")
            if c:
                out[c] = int(float(r["value"][1]))
        return out

    def lines(self, container, start, end, cap=None):
        """The container's error-ish lines in [start, end), oldest first: ([(ns, line)], capped)."""
        cap = cap or LINE_CAP
        q = '{job="docker", container="%s"} |~ "%s"' % (_esc(container), _esc(LOGQL_ERR))
        got, lo, hi = [], int(start * 1e9), int(end * 1e9)
        while lo < hi and len(got) < cap:
            d = self.get("/loki/api/v1/query_range", query=q, start=str(lo), end=str(hi), limit=str(PAGE),
                         direction="forward")
            page = []
            for st in (d.get("data") or {}).get("result") or []:
                for ts, line in st.get("values") or []:
                    page.append((int(ts), line))
            if not page:
                break
            page.sort(key=lambda x: x[0])
            got.extend(page)
            if len(page) < PAGE:
                break
            lo = page[-1][0] + 1
        return got[:cap], len(got) >= cap


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------------------------------- one day
def digest_dir():
    p = os.environ.get("ERROR_DIGEST_DIR")
    if p:
        return Path(p).expanduser()
    try:
        import queue_config
        return Path(queue_config.dispatch_dir()) / "error-digest"
    except Exception:
        return Path.home() / ".ollama-dispatch" / "error-digest"


def day_bounds(day):
    """[start, end) of a local calendar day (container TZ) as unix seconds."""
    d = dt.date.fromisoformat(day)
    start = dt.datetime(d.year, d.month, d.day).astimezone()
    end = (dt.datetime(d.year, d.month, d.day) + dt.timedelta(days=1)).astimezone()
    return start.timestamp(), end.timestamp()


def history(ddir, day, days=LOOKBACK_DAYS):
    """({container: set(sig ids)}, [days found]) from the digests of the `days` days before `day`."""
    seen, found = {}, []
    d0 = dt.date.fromisoformat(day)
    for k in range(1, days + 1):
        dd = (d0 - dt.timedelta(days=k)).isoformat()
        try:
            rec = json.loads((Path(ddir) / "digests" / (dd + ".json")).read_text())
        except (OSError, ValueError):
            continue
        found.append(dd)
        for c, info in (rec.get("containers") or {}).items():
            seen.setdefault(c, set()).update(s.get("id") for s in info.get("sigs") or [])
    return seen, sorted(found)


REPEAT_DAYS = 7             # an owner item also seen on any of these days before is "ongoing", not new


def alert_history(ddir, day, days=REPEAT_DAYS):
    """{rule "what": number of the `days` days before `day` whose digest had it for the owner or held back}."""
    out = {}
    d0 = dt.date.fromisoformat(day)
    for k in range(1, days + 1):
        dd = (d0 - dt.timedelta(days=k)).isoformat()
        try:
            al = json.loads((Path(ddir) / "digests" / (dd + ".json")).read_text()).get("alerts") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for w in {i.get("what") for i in (al.get("owner") or []) + (al.get("held") or [])}:
            out[w] = out.get(w, 0) + 1
    return out


def summarize_lines(container, rows, rules):
    """rows [(ns, line)] -> container record: kinds with counts, routine/skipped/benign/fragment counts."""
    sigs, routine, skipped, benign, frags = {}, {}, 0, 0, 0
    for ns, raw in rows:
        line = unglue(clean(raw))
        act = noise_action(rules, container, line)
        if act == "skip":
            skipped += 1
            continue
        if is_fragment(line):
            frags += 1
            continue
        lv = level(line)
        if lv is None:
            benign += 1
            continue
        s = signature(line)
        if act == "routine":
            routine[s] = routine.get(s, 0) + 1
            continue
        sid = sig_id(container, s)
        r = sigs.get(sid)
        if r is None:
            r = sigs[sid] = {"id": sid, "text": shape(line), "level": lv, "count": 0, "first_ns": ns, "last_ns": ns,
                             "_v": set()}
        r["count"] += 1
        if len(r["_v"]) < 50:
            r["_v"].add(shape(line))
        r["last_ns"] = max(r["last_ns"], ns)
        if lv == "error":
            r["level"] = "error"
    for r in sigs.values():
        r["variants"] = len(r.pop("_v"))
    return {"lines": len(rows), "routine": sum(routine.values()), "routine_kinds": len(routine), "skipped": skipped,
            "benign": benign, "fragments": frags, "sigs": sorted(sigs.values(), key=lambda r: (-r["count"], r["id"]))}


# Docker's own name generator (adjective_surname) names a container started without --name: a one-off run such as a
# tile build. Their lines go under one heading, so the same one-off job run twice is not two sets of "new" kinds.
ONE_OFF = "(one-off containers)"
_DOCKER_ADJ = set("""admiring adoring affectionate agitated amazing angry awesome beautiful blissful bold boring brave busy
charming clever compassionate competent condescending confident cool cranky crazy dazzling determined distracted dreamy
eager ecstatic elastic elated elegant eloquent epic exciting fervent festive flamboyant focused friendly frosty funny
gallant gifted goofy gracious great happy hardcore heuristic hopeful hungry infallible inspiring intelligent interesting
jolly jovial keen kind laughing loving lucid magical modest musing mystifying naughty nervous nice nifty nostalgic
objective optimistic peaceful pedantic pensive practical priceless quirky quizzical recursing relaxed reverent romantic
sad serene sharp silly sleepy stoic strange stupefied suspicious sweet tender thirsty trusting unruffled upbeat vibrant
vigilant vigorous wizardly wonderful xenodochial youthful zealous zen""".split())


def group_of(container):
    a, _, b = (container or "").partition("_")
    return ONE_OFF if a in _DOCKER_ADJ and re.fullmatch(r"[a-z]+\d*", b or "") else container


def build_day(loki, day, ddir, rules, say=print, arules=()):
    start, end = day_bounds(day)
    counts = loki.error_counts(start, end)
    seen, hist_days = history(ddir, day)
    rec = {"day": day, "window": [_iso(start), _iso(end)], "generated": _iso(time.time()),
           "history_days": hist_days, "baseline": not hist_days, "containers": {}, "model": None}
    groups, acc = {}, {}
    for c in sorted(counts, key=lambda k: -counts[k]):
        if counts[c] > 0:
            groups.setdefault(group_of(c), []).append(c)
    for c, names in groups.items():
        rows, capped = [], False
        for n in names:
            r, cp = loki.lines(n, start, end)
            rows += r
            capped = capped or cp
        rows.sort(key=lambda x: x[0])
        info = summarize_lines(c, rows, rules)
        for i, h in match_alerts(c, rows, arules).items():
            acc.setdefault(i, []).append((c, h))
        info["capped"] = capped
        info["loki_count"] = sum(counts[n] for n in names)
        if c == ONE_OFF:
            info["names"] = sorted(names)
        prior = seen.get(c, set())
        for s in info["sigs"]:
            s["new"] = bool(hist_days) and s["id"] not in prior
        if info["sigs"] or info["routine"] or info["skipped"] or info["fragments"]:
            rec["containers"][c] = info
        say("[error-digest] %s %s: %d lines, %d kinds, %d new%s" % (day, c, info["lines"], len(info["sigs"]),
            sum(1 for s in info["sigs"] if s["new"]), " (capped)" if capped else ""))
    if arules:
        rec["alerts"] = decide_alerts(acc, list(arules), rec["containers"],
                                      before=_before_midnight(loki, start, acc, list(arules), groups))
        prev = alert_history(ddir, day)
        for it in rec["alerts"]["owner"] + rec["alerts"]["held"]:
            it["seen_days"] = prev.get(it["what"], 0)
    return rec


def _before_midnight(loki, start, acc, arules, groups):
    """{rule index: [ns]}: for a sustain rule whose first line today is within its gap of midnight, its lines in the
    sustain_min minutes before midnight (enough to tell whether the run that crossed midnight lasted that long).
    A Loki error here only loses the look back, never the digest."""
    out = {}
    for i, per in acc.items():
        a = arules[i]
        first = min(t for _, h in per for t in h["ts"])
        if not a["sustain_min"] or first - start * 1e9 > a["gap_min"] * 60e9:
            continue
        for c, _ in per:
            rows = []
            try:
                for n in groups.get(c) or [c]:
                    rows += loki.lines(n, start - a["sustain_min"] * 60, start)[0]
            except LokiError:
                continue
            h = match_alerts(c, sorted(rows, key=lambda x: x[0]), arules).get(i)
            if h:
                out.setdefault(i, []).extend(h["ts"])
    return out


def _iso(t):
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _hm(ns):
    return dt.datetime.fromtimestamp(ns / 1e9).astimezone().strftime("%H:%M")


# ---------------------------------------------------------------------------------------------------- rendering
SHOW_NEW_ERRORS, SHOW_NEW_WARNINGS = 10, 5       # per container in "New today"; the rest are counted, kept in the JSON


def render(rec, top=5):
    day = rec["day"]
    cs = rec.get("containers") or {}
    meaning = ((rec.get("model") or {}).get("lines") or {})
    new = [(c, s) for c, i in cs.items() for s in i["sigs"] if s.get("new")]
    new.sort(key=lambda x: (x[1]["level"] != "error", -x[1]["count"]))
    n_err_lines = sum(s["count"] for i in cs.values() for s in i["sigs"])
    n_kinds = sum(len(i["sigs"]) for i in cs.values())
    wd = dt.date.fromisoformat(day).strftime("%a")
    out = ["# Container errors, %s %s" % (wd, day), ""]
    if rec.get("baseline"):
        head = "First day on record, so nothing is marked new yet. "
    else:
        ne = sum(1 for _, s in new if s["level"] == "error")
        head = "%s new kind%s of error%s. " % (ne or "No", "" if ne == 1 else "s",
                                               (", %d new warning%s" % (len(new) - ne, "" if len(new) - ne == 1 else "s"))
                                               if len(new) - ne else "")
    out.append(head + "%s error and warning lines in %d kinds, from %d container%s." % (
        f"{n_err_lines:,}", n_kinds, len(cs), "" if len(cs) == 1 else "s"))
    m = rec.get("model") or {}
    if m.get("status") == "pending":
        out.append("_The model's notes are still being written._")
    elif m.get("status") == "failed":
        out.append("_No model notes today (the model job failed); plain counts only._")
    out.append("")
    out += render_alerts(rec.get("alerts"))
    out += render_extra(rec.get("extra"))
    if new:
        out += ["## New today", ""]
        by_c = {}
        for c, s in new:
            by_c.setdefault(c, []).append(s)
        for c, ss in sorted(by_c.items(), key=lambda kv: (all(s["level"] != "error" for s in kv[1]),
                                                         -max(s["count"] for s in kv[1]))):
            out += ["### %s" % c + (" (%s)" % ", ".join(cs[c]["names"]) if cs[c].get("names") else ""), ""]
            errs = [s for s in ss if s["level"] == "error"]
            warns = [s for s in ss if s["level"] != "error"]
            shown = errs[:SHOW_NEW_ERRORS] + warns[:SHOW_NEW_WARNINGS]
            for s in shown:
                when = _hm(s["first_ns"]) if s["count"] == 1 else "%s to %s" % (_hm(s["first_ns"]), _hm(s["last_ns"]))
                out.append("- %s, %s×, %s: `%s`" % (s["level"], f"{s['count']:,}", when, _short(s["text"])))
                if meaning.get(s["id"]):
                    out.append("  Probably: " + meaning[s["id"]])
            more_e, more_w = len(errs) - len([s for s in shown if s["level"] == "error"]), len(ss) - len(shown)
            more_w -= more_e
            if more_e or more_w:
                bits = ["%d rarer new %s kind%s" % (n, what, "" if n == 1 else "s")
                        for what, n in (("error", more_e), ("warning", more_w)) if n]
                out.append("- plus %s (all in %s.json)" % (" and ".join(bits), day))
            out.append("")
    if rec.get("plex_glacier"):
        try:
            import plex_glacier_watch
            out += plex_glacier_watch.render_section(rec["plex_glacier"])
        except Exception:  # noqa: BLE001 - the error part of the digest must not depend on the Plex section
            pass
    if rec.get("playback"):
        try:
            import plex_playback
            out += plex_playback.render_section(rec["playback"])
        except Exception:  # noqa: BLE001 - as above
            pass
    out += ["## By container", "", "| Container | Lines | Kinds | New | Most frequent |", "|---|---|---|---|---|"]
    for c, i in sorted(cs.items(), key=lambda kv: -sum(s["count"] for s in kv[1]["sigs"])):
        lines = sum(s["count"] for s in i["sigs"])
        if not i["sigs"]:
            continue
        t = i["sigs"][0]
        out.append("| %s | %s%s | %d | %d | %d× `%s` |" % (
            c, f"{lines:,}", "+" if i.get("capped") else "", len(i["sigs"]), sum(1 for s in i["sigs"] if s.get("new")),
            t["count"], t["text"][:90].replace("|", "/").replace("`", "'")))
    folded = [(c, i) for c, i in cs.items() if i.get("routine") or i.get("skipped") or i.get("fragments")]
    if folded:
        out += ["", "## Known noise (folded)", ""]
        for c, i in folded:
            bits = []
            if i.get("routine"):
                bits.append("%s routine line%s in %d kind%s" % (f"{i['routine']:,}", "" if i["routine"] == 1 else "s",
                                                                 i["routine_kinds"], "" if i["routine_kinds"] == 1 else "s"))
            if i.get("skipped"):
                bits.append("%s left out (alerted elsewhere)" % f"{i['skipped']:,}")
            if i.get("fragments"):
                bits.append("%s stack-trace or page-markup line%s (the error lines they belong to are listed)" % (
                    f"{i['fragments']:,}", "" if i["fragments"] == 1 else "s"))
            out.append("- %s: %s" % (c, ", ".join(bits)))
    return "\n".join(out).rstrip() + "\n"


def _when(item):
    n = item["count"]
    w = "%s×, %s" % (f"{n:,}", _hm(item["first_ns"]) if n == 1 else "%s to %s" % (_hm(item["first_ns"]), _hm(item["last_ns"])))
    if item.get("seen_days"):
        w += "; also on %d of the last %d days" % (item["seen_days"], REPEAT_DAYS)
    return w


def render_alerts(al):
    """The "For you" / "Held back" / "For Claude" sections (empty list when the day has no routing)."""
    if not al:
        return []
    out = ["## For you", ""]
    for it in al["owner"]:
        out.append("- **%s** (%s; %s)" % (it["what"], ", ".join(it["containers"]), _when(it)))
        out.append("  e.g. `%s`" % _short(it["sample"]))
    if not al["owner"]:
        out.append("Nothing needs you today.")
    out.append("")
    if al["held"]:
        out += ["## Held back (not sent)", ""]
        for it in al["held"]:
            out.append("- %s (%s; %s): %s" % (it["what"], ", ".join(it["containers"]), _when(it), it["held"]))
        out.append("")
    if al["claude"] or al["new_for_claude"]:
        out += ["## For Claude", ""]
        for it in al["claude"]:
            out.append("- %s (%s; %s)" % (it["what"], ", ".join(it["containers"]), _when(it)))
        if al["new_for_claude"]:
            out.append("- new kinds no rule covers yet:")
            for n in al["new_for_claude"]:
                out.append("  - %s, %s×: `%s`" % (n["container"], f"{n['count']:,}", _short(n["text"])))
            if al.get("new_for_claude_more"):
                out.append("  - plus %d more (in the JSON)" % al["new_for_claude_more"])
        out.append("")
    return out


def render_extra(extra):
    """One section per app that posted count lines for the day (digest_lines.py); [] when none did."""
    out = []
    for e in extra or []:
        out += ["## %s%s" % (e["title"], " (needs you)" if e.get("needs_you") else ""), ""]
        out += ["- %s" % _short(x, 200) for x in e["lines"]]
        out.append("")
    return out


def notify_text(rec):
    """(subject, text) of the daily message to the operator: their items first (example lines through mail_sample(),
    none for a names rule), the apps' lines and Playback, then counts only (held items too: they are not sent)."""
    al = rec.get("alerts") or {}
    day = rec["day"]
    wd = dt.date.fromisoformat(day).strftime("%a %b %-d")
    mine, held = al.get("owner") or [], al.get("held") or []
    fresh = sum(1 for i in mine if not i.get("seen_days"))
    extra = rec.get("extra") or []
    asks = [e["title"] for e in extra if e.get("needs_you")]
    subj = "Unraid %s: %s" % (wd, "nothing needs you" if not mine else "%d thing%s for you%s" % (
        len(mine), "" if len(mine) == 1 else "s",
        "" if fresh == len(mine) else ", %s new" % (fresh or "none")))
    if asks:
        subj = "Unraid %s: %s needs you%s" % (wd, " and ".join(asks), "" if not mine else "; %d thing%s for you" % (
            len(mine), "" if len(mine) == 1 else "s"))
    mine = sorted(mine, key=lambda i: bool(i.get("seen_days")))      # new first
    t = []
    for it in mine:
        t.append("- %s (%s; %s)" % (it["what"], ", ".join(it["containers"]), _when(it)))
        if not it.get("names"):            # a line that names a download or file stays in the digest on Unraid
            t.append("    e.g. %s" % _short(mail_sample(it["sample"]), 200).replace("`", "'"))
        if it.get("do"):
            t.append("    Do: %s" % it["do"])
    if not mine:
        t.append("No errors need you today." if extra else "Nothing needs you today.")
    for e in extra:
        t += ["", "%s%s:" % (e["title"], " (needs you)" if e.get("needs_you") else "")]
        t += ["- %s" % _short(x, 200) for x in e["lines"]]
    if (rec.get("playback") or {}).get("spells"):
        try:
            import plex_playback
            t += ["", "Plex buffering (stalls, not skips):"] + ["- %s" % _short(x, 240)
                                                                for x in plex_playback.notify_lines(rec["playback"])]
        except Exception:  # noqa: BLE001 - the message goes out without it
            pass
    if held:                                # below the bar its rule sets: named with the reason, no example line
        t += ["", "Below the bar (no action needed):"]
        t += ["- %s (%s): %s" % (it["what"], _when(it), it["held"]) for it in held]
    cs = rec.get("containers") or {}
    n_lines = sum(sc["count"] for i in cs.values() for sc in i["sigs"])
    nc = len(al.get("claude") or []) + len(al.get("new_for_claude") or []) + (al.get("new_for_claude_more") or 0)
    t += ["", "Also logged: %s error/warning lines from %d container%s; %d item%s for Claude to look at (not for you)." % (
        f"{n_lines:,}", len(cs), "" if len(cs) == 1 else "s", nc, "" if nc == 1 else "s")]
    t.append("Full digest: error-digest/digests/%s.md on Unraid." % day)
    return subj, "\n".join(t) + "\n"


# The HTML twin of notify_text: the same facts in the same order. Tables and inline styles only (mail clients drop
# <style> rules often enough); the few classes are for the dark-mode block, which clients that honour it apply.
_C = {"ink": "#1d2733", "soft": "#5b6875", "ground": "#eef1f4", "paper": "#ffffff", "rule": "#d9dfe5",
      "act": "#b25e00", "act_bg": "#fff6e9", "calm": "#2f6b4f"}
_FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _e(s):
    return html.escape(str(s), quote=True)


def notify_html(rec):
    """The daily message as HTML (the mail Worker sends it next to the plain text; a Worker that does not know the
    field sends the text alone). Same rules as notify_text: no example line for a names rule, held items named with
    their reason, no container log text beyond mail_sample()."""
    al = rec.get("alerts") or {}
    wd = dt.date.fromisoformat(rec["day"]).strftime("%A, %B %-d")
    mine = sorted(al.get("owner") or [], key=lambda i: bool(i.get("seen_days")))
    held, extra = al.get("held") or [], rec.get("extra") or []
    asks = [e["title"] for e in extra if e.get("needs_you")]
    n = len(mine) + len(asks)
    verdict = "Nothing needs you" if not n else "%d thing%s need%s you" % (n, "" if n == 1 else "s", "s" if n == 1 else "")
    bar = _C["act"] if n else _C["calm"]
    o = ['<!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">'
         '<meta name="viewport" content="width=device-width,initial-scale=1"><style>'
         '@media (prefers-color-scheme:dark){.g{background:#12181f!important}.p{background:#1a222c!important}'
         '.i{color:#e6ebf0!important}.s{color:#9aa7b4!important}.a{background:#2b2113!important}'
         '.r{border-color:#2c3743!important}}</style></head>',
         '<body class="g" style="margin:0;padding:0;background:%s">' % _C["ground"],
         '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" class="g" style="background:%s">'
         '<tr><td align="center" style="padding:20px 12px">' % _C["ground"],
         '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" class="p" style="max-width:600px;'
         'background:%s;border-top:6px solid %s;font-family:%s">' % (_C["paper"], bar, _FONT),
         '<tr><td style="padding:26px 28px 8px"><div class="s" style="font-size:14px;color:%s">Unraid, %s</div>'
         '<div class="i" style="font-size:30px;line-height:1.2;font-weight:700;color:%s;padding-top:4px">%s</div>'
         '</td></tr>' % (_C["soft"], _e(wd), _C["ink"], _e(verdict))]

    def section(title):
        o.append('<tr><td style="padding:22px 28px 0"><div class="i r" style="font-size:16px;font-weight:700;color:%s;'
                 'border-bottom:1px solid %s;padding-bottom:6px">%s</div></td></tr>' % (_C["ink"], _C["rule"], _e(title)))

    def lines(items, pad="8px 28px 0"):
        o.append('<tr><td class="i" style="padding:%s;font-size:15px;line-height:1.5;color:%s">%s</td></tr>' % (
            pad, _C["ink"], "".join('<div style="padding:3px 0">%s</div>' % x for x in items)))

    for it in mine:
        where = "%s, %s" % (", ".join(it["containers"]), _when(it))
        body = ['<div class="i" style="font-size:17px;font-weight:700;color:%s">%s</div>' % (_C["ink"], _e(it["what"])),
                '<div class="s" style="font-size:13px;color:%s;padding-top:2px">%s</div>' % (_C["soft"], _e(where))]
        if it.get("do"):
            body.append('<div class="i" style="font-size:15px;color:%s;padding-top:8px"><b>Do this:</b> %s</div>' % (
                _C["ink"], _e(it["do"])))
        if not it.get("names"):
            body.append('<div class="s" style="font-size:12px;color:%s;padding-top:8px;font-family:Menlo,Consolas,'
                        'monospace;word-break:break-word">%s</div>' % (
                            _C["soft"], _e(_short(mail_sample(it["sample"]), 200))))
        o.append('<tr><td style="padding:14px 28px 0"><table role="presentation" width="100%%" cellpadding="0" '
                 'cellspacing="0"><tr><td class="a" style="background:%s;border-left:4px solid %s;padding:14px 16px">'
                 '%s</td></tr></table></td></tr>' % (_C["act_bg"], _C["act"], "".join(body)))
    if not mine:
        lines(['<span class="s" style="color:%s">No container errors need you today.</span>' % _C["soft"]], "10px 28px 0")
    for e in extra:
        section(e["title"] + (" (needs you)" if e.get("needs_you") else ""))
        lines([_e(_short(x, 200)) for x in e["lines"]])
    if (rec.get("playback") or {}).get("spells"):
        try:
            import plex_playback
            pl = [_e(_short(x, 240)) for x in plex_playback.notify_lines(rec["playback"])]
            section("Plex buffering")
            lines(pl)
        except Exception:  # noqa: BLE001 - the message goes out without it
            pass
    if held:
        section("Below the bar, no action needed")
        lines(['%s <span class="s" style="color:%s">(%s; %s)</span>' % (
            _e(it["what"]), _C["soft"], _e(_when(it)), _e(it["held"])) for it in held])
    cs = rec.get("containers") or {}
    n_lines = sum(sc["count"] for i in cs.values() for sc in i["sigs"])
    nc = len(al.get("claude") or []) + len(al.get("new_for_claude") or []) + (al.get("new_for_claude_more") or 0)
    o.append('<tr><td class="s r" style="padding:22px 28px 26px;font-size:13px;line-height:1.5;color:%s">'
             '<div class="r" style="border-top:1px solid %s;padding-top:12px">%s error and warning lines from %d '
             'container%s. %d item%s went to Claude, not to you. Full digest on Unraid: error-digest/digests/%s.md'
             '</div></td></tr>' % (_C["soft"], _C["rule"], f"{n_lines:,}", len(cs), "" if len(cs) == 1 else "s", nc,
                                   "" if nc == 1 else "s",
                                   _e(rec["day"])))
    o.append("</table></td></tr></table></body></html>")
    return "".join(o)


def notify_settings():
    """(url, token) for the daily message, from $ERROR_DIGEST_NOTIFY_URL and $ERROR_DIGEST_NOTIFY_TOKEN (or the file
    $ERROR_DIGEST_NOTIFY_TOKEN_FILE). Either one unset: shadow (the message is only written to <day>.notify.txt)."""
    url = os.environ.get("ERROR_DIGEST_NOTIFY_URL", "").strip()
    tok = os.environ.get("ERROR_DIGEST_NOTIFY_TOKEN", "").strip()
    tf = os.environ.get("ERROR_DIGEST_NOTIFY_TOKEN_FILE", "").strip()
    if not tok and tf:
        try:
            tok = Path(tf).read_text().strip()
        except OSError:
            tok = ""
    return url, tok


NOTIFY_WAITS = (30,)        # a send that failed on the network, a 429 or a 5xx is tried again after these many seconds
                            # (short: a stop during the wait loses the run; longer outages are cmd_tick's resends)


def send_notify(rec, waits=None):
    """POST {app, ref, subject, text} with the bearer token; -> the rec["notify"] record. Never raises, never prints
    the token or the URL's query. The token is never passed on to a redirect's target. A 409 is the mail Worker saying
    this day was already mailed: recorded as sent. A failure worth trying again later is marked retry_later."""
    url, tok = notify_settings()
    now = _iso(time.time())
    if not url or not tok:
        return {"status": "shadow", "at": now}
    if not url.startswith("https://"):
        return {"status": "failed", "at": now, "why": "the notify URL must be https"}
    subj, text = notify_text(rec)
    body = {"app": "error-digest", "ref": rec["day"], "subject": subj, "text": text}
    try:
        body["html"] = notify_html(rec)
    except Exception:  # noqa: BLE001 - the plain text goes out without its HTML twin
        pass
    body = json.dumps(body).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "error-digest"})
    req.add_unredirected_header("Authorization", "Bearer " + tok)
    out, tries, again = {}, 0, False
    for wait in (0,) + tuple(NOTIFY_WAITS if waits is None else waits):
        if wait:
            time.sleep(wait)
        tries += 1
        out = _post_once(req)
        again = out.pop("retry", False)
        if not again:
            break
    return dict(out, at=now, **({"tries": tries} if tries > 1 else {}), **({"retry_later": True} if again else {}))


def _post_once(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (ValueError, http.client.InvalidURL) as e:   # a malformed URL, port or token in the settings: no retry
        return {"status": "failed", "why": "bad notify settings (%s)" % type(e).__name__}   # fixes it (InvalidURL is
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:   # an HTTPException, so it goes first)
        return {"status": "failed", "why": str(getattr(e, "reason", e))[:200], "retry": True}   # network: retry later
    if 200 <= code < 300:
        return {"status": "sent", "http": code}
    if code == 409:
        return {"status": "sent", "http": code, "why": "already mailed for this day"}
    return {"status": "failed", "http": code, "retry": code == 429 or code >= 500}


def _short(t, n=150):
    t = t.replace("`", "'")
    return t if len(t) <= n else t[:n - 1] + "…"


def write_day(ddir, rec):
    d = Path(ddir) / "digests"
    d.mkdir(parents=True, exist_ok=True)
    files = [(rec["day"] + ".json", json.dumps(rec, indent=1)), (rec["day"] + ".md", render(rec))]
    if rec.get("alerts"):
        subj, text = notify_text(rec)
        files.append((rec["day"] + ".notify.txt", "Subject: %s\n\n%s" % (subj, text)))
    for name, text in files:
        tmp = d / (name + ".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, d / name)
    return d / (rec["day"] + ".md")


# ---------------------------------------------------------------------------------------------------- model step
def model_items(rec, per_container=MODEL_SIGS_PER_CONTAINER):
    """{container: [sig]} the model is asked about: the NEW kinds, errors first, at most `per_container` each."""
    out = {}
    for c, i in (rec.get("containers") or {}).items():
        new = [s for s in i["sigs"] if s.get("new")]
        new.sort(key=lambda s: (s["level"] != "error", -s["count"]))
        if new:
            out[c] = new[:per_container]
    return out


def enqueue_model_job(ddir, day, model, say=print):
    """One queue job (Unraid lane) that adds the 'probably means' lines. Returns the job id or None."""
    task = Path(ddir) / "jobs" / (day + ".task.json")
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(json.dumps({"day_json": str(Path(ddir) / "digests" / (day + ".json"))}))
    cmd = [sys.executable, os.path.join(HERE, "ollama-queue.py"), "enqueue", "--model", model, "--host", "unraid",
           "--runner", os.path.join(HERE, "error-digest-runner.py"), "--task-file", str(task),
           "--label", "error-digest-" + day.replace("-", ""), "--cwd", str(ddir), "--task-kind", "research",
           "--num-ctx", "8192"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        say("[error-digest] model job not queued (%s); plain counts only" % type(e).__name__)
        return None
    m = re.search(r"^enqueued ([0-9a-f]{12})\b", r.stdout or "", re.M)
    if r.returncode != 0 or not m:
        say("[error-digest] model job not queued (rc %s); plain counts only" % r.returncode)
        return None
    try:                                        # the tick's sign that this day's job was queued (_requeue_model)
        _queued_marker(ddir, day).write_text(m.group(1) + "\n")
    except OSError:
        pass
    return m.group(1)


def _queued_marker(ddir, day):
    return Path(ddir) / "jobs" / (day + ".queued")


def _queue_model(ddir, day, model, rec, say=print):
    """Queue the model job for a digest already written with model "pending"; when it cannot be queued, record that
    (re-read under the notify lock, like every write after the first)."""
    jid = enqueue_model_job(ddir, day, model, say=say)
    if jid:
        say("[error-digest] model job %s queued" % jid)
        return
    failed = {"status": "failed", "model": model, "why": "not queued"}
    with _notify_lock(ddir, True):
        cur = _read_day(ddir, day) or rec
        if cur.get("day") == day:
            cur["model"] = failed
            write_day(ddir, cur)
    rec["model"] = failed


# ---------------------------------------------------------------------------------------------------- CLI
def _yesterday():
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _arg(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def noise_rules(ddir):
    """The shipped noise rules (from the image) plus the digest dir's own noise.json, which is checked first. Same rule
    twice (an old copy of the shipped file) counts once."""
    out, seen = [], set()
    for path in (Path(ddir) / "noise.json", Path(HERE) / "error_digest_noise.json"):
        for r in load_noise(path):
            if (r[0], r[1].pattern, r[2]) not in seen:
                seen.add((r[0], r[1].pattern, r[2]))
                out.append(r)
    return out


def alert_rules(ddir):
    """The digest dir's own alerts.json (checked first) plus the shipped error_digest_alerts.json."""
    return load_alerts(Path(ddir) / "alerts.json", Path(HERE) / "error_digest_alerts.json")


def cmd_run(argv, days=None):
    ddir = digest_dir()
    rules, arules = noise_rules(ddir), alert_rules(ddir)
    url, tok = loki_settings()
    try:
        loki = Loki(url, tok)
    except LokiError as e:
        print("error-digest: %s" % e, file=sys.stderr)
        return 2
    model = None if "--no-model" in argv else (_arg(argv, "--model") or os.environ.get("ERROR_DIGEST_MODEL")
                                              or MODEL_DEFAULT)
    send = days is None and "--no-send" not in argv      # never from backfill
    yday = _yesterday()            # fixed at the start: a build that runs past midnight still sends the day it built
    if days is None:
        days = [_arg(argv, "--day") or yday]
    for day in days:
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            print("error-digest: bad --day %r (YYYY-MM-DD)" % day, file=sys.stderr)
            return 2
        try:
            rec = build_day(loki, day, ddir, rules, arules=arules)
        except LokiError as e:
            print("error-digest: %s: %s" % (day, e), file=sys.stderr)
            return 1
        try:
            import plex_glacier_watch
            pg = plex_glacier_watch.day_summary(day)
            if pg:
                rec["plex_glacier"] = pg
        except Exception:  # noqa: BLE001 - see render()
            pass
        try:
            import plex_playback
            pb = plex_playback.day_summary(loki, day)
            if pb:
                rec["playback"] = pb
        except Exception as e:  # noqa: BLE001 - the digest goes out without it
            rec["playback_error"] = type(e).__name__
        try:
            import digest_lines
            ex = digest_lines.read_day(day, ddir)
            if ex:
                rec["extra"] = ex
        except Exception:  # noqa: BLE001 - the digest goes out without them
            pass
        want_model = bool(model and model_items(rec))
        if want_model:
            rec["model"] = {"status": "pending", "model": model}
        sending = False
        with _notify_lock(ddir, rec.get("alerts") and send):   # two runs at once: one sends, the other sees "sent"
            if rec.get("alerts") and send:
                prev = _prev_notify(ddir, day)
                if prev.get("status") == "sent":
                    rec["notify"] = prev                      # one message per day, whatever reruns there are
                elif day == yday:
                    rec["notify"] = {"status": "pending", "at": _iso(time.time())}
                    sending = True
            # the first write: a stop after it keeps the digest, and a tick finishes the rest (the model job, the send)
            out = write_day(ddir, rec)
        print("[error-digest] wrote %s" % out)
        if want_model:
            # queued before the send, so a stop during the send leaves only the send to finish. From here on the digest
            # is only rewritten re-read, under the notify lock: the job may finish first, its lines must stay
            _queue_model(ddir, day, model, rec)
        if sending:
            with _notify_lock(ddir, True):
                cur = _read_day(ddir, day)
                if (cur.get("notify") or {}).get("status") == "pending":   # else a tick or another run sent it meanwhile
                    st = _store_sent(ddir, day, cur, send_notify(cur))
                    print("[error-digest] daily message: %s" % st)
    return 0


def _store_sent(ddir, day, rec, sent):
    """Write a send's result into the stored day, re-read first so only notify changes (the model job may have written
    its lines during the send). Call under the notify lock. -> the status."""
    rec = _read_day(ddir, day) or rec
    rec["notify"] = sent
    write_day(ddir, rec)
    return sent["status"]


TICK_AT = (5, 7)            # cmd_tick builds yesterday on its first tick at or after this local time (hour, minute)
TICK_MAX_BUILDS = 12        # full builds of one day at most (a run that dies before it writes must not loop all day)
BUILD_EVERY_S = 900         # ...and a build is tried again only this long after the last try (12 x 15 min: ~3 h of
                            # Loki outage; after that the day is built by hand: error_digest.py run)
RESEND_EVERY_S = 1800       # a send that failed for a reason worth retrying is sent again this long after the last try


def cmd_tick(argv):
    """The container's daily run (supervisord: every.sh 300 ... tick). On its first tick at or after TICK_AT it builds
    and sends yesterday (cmd_run). Later ticks only finish what a stop or an outage left: no digest for yesterday yet
    (Loki was down, or the run was stopped before it wrote) -> build again BUILD_EVERY_S after the last try, at most
    TICK_MAX_BUILDS times; a send left pending (the run was stopped during it) -> send from the stored digest; a failed
    send marked retry_later -> send again every RESEND_EVERY_S until midnight; a run of this tick stopped before it
    queued its model job -> queued once. A digest that exists is never rebuilt (that would drop the model job's lines).
    A finished day costs a few small file reads per tick."""
    now = dt.datetime.now()
    if (now.hour, now.minute) < tuple(TICK_AT):
        return 0
    ddir, day, rc = digest_dir(), _yesterday(), 0
    st = _tick_state(ddir, day)
    if not st.get("done"):
        if not (Path(ddir) / "digests" / (day + ".json")).exists():
            if st.get("builds", 0) >= TICK_MAX_BUILDS or time.time() - st.get("last", 0) < BUILD_EVERY_S:
                return 0
            st["builds"], st["last"] = st.get("builds", 0) + 1, time.time()
            _tick_save(ddir, st)
            rc = cmd_run(argv)
            if rc == 1:                     # Loki failed part way: the next tick tries again
                return rc
        st["done"] = True                   # built, or a settings problem (rc 2) a retry today will not fix
        _tick_save(ddir, st)
    _requeue_model(ddir, day, st)
    return _resend(ddir, day) or rc


def _requeue_model(ddir, day, st):
    """A day this tick built whose run was stopped after its first write but before its model job was queued (the
    digest says "pending", no queued marker): queue the job, once. Days built otherwise (an older image's 05:07 run,
    a run by hand) are left alone: their job may well be queued."""
    if not st.get("builds") or st.get("model_requeued") or _queued_marker(ddir, day).exists():
        return
    m = _read_day(ddir, day).get("model") or {}
    if m.get("status") != "pending" or not m.get("model"):
        return
    st["model_requeued"] = True
    _tick_save(ddir, st)
    _queue_model(ddir, day, m["model"], {})


def _resend(ddir, day):
    """Send yesterday's stored message again when it is due (see cmd_tick). Under the notify lock, so a run that is
    sending right now is waited for; a "pending" seen under the lock is a run that died during its send. One try."""
    if not (Path(ddir) / "digests" / (day + ".json")).exists():
        return 0
    with _notify_lock(ddir, True):
        rec = _read_day(ddir, day)
        n = rec.get("notify") or {}
        due = n.get("status") == "pending" or (n.get("status") == "failed" and n.get("retry_later")
                                                and time.time() - _parse_iso(n.get("at")) >= RESEND_EVERY_S)
        if not (rec.get("alerts") and rec.get("day") == day and due):
            return 0
        st = _store_sent(ddir, day, rec, dict(send_notify(rec, waits=()), resent=True))
        print("[error-digest] daily message (resent): %s" % st)
    return 0


def _read_day(ddir, day):
    try:
        rec = json.loads((Path(ddir) / "digests" / (day + ".json")).read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_iso(t):
    try:
        return dt.datetime.fromisoformat(t).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _tick_state(ddir, day):
    try:
        st = json.loads((Path(ddir) / ".tick.json").read_text())
        return st if isinstance(st, dict) and st.get("day") == day else {"day": day}
    except (OSError, ValueError):
        return {"day": day}


def _tick_save(ddir, st):
    Path(ddir).mkdir(parents=True, exist_ok=True)
    tmp = Path(ddir) / ".tick.json.tmp"
    tmp.write_text(json.dumps(st))
    os.chmod(tmp, 0o600)
    os.replace(tmp, Path(ddir) / ".tick.json")


@contextlib.contextmanager
def _notify_lock(ddir, want):
    """An exclusive lock on <digest dir>/.notify.lock around check, send and write (no-op when nothing is sent)."""
    if not want:
        yield
        return
    import fcntl
    Path(ddir).mkdir(parents=True, exist_ok=True)
    fd = os.open(str(Path(ddir) / ".notify.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _prev_notify(ddir, day):
    try:
        return json.loads((Path(ddir) / "digests" / (day + ".json")).read_text()).get("notify") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def cmd_backfill(argv):
    n = int(_arg(argv, "--days", "14"))
    today = dt.date.today()
    days = [(today - dt.timedelta(days=k)).isoformat() for k in range(n, 0, -1)]
    return cmd_run(argv, days=days)


def cmd_show(argv):
    day = _arg(argv, "--day") or _yesterday()
    if _use_remote():
        status, obj = fetch_remote({"day": day})
        if status != 200:
            print("error-digest: %s: %s" % (day, _remote_why(status, obj)), file=sys.stderr)
            return 1
        sys.stdout.write(obj.get("md") or "")
        if obj.get("md_truncated"):
            print("\n(cut at %d characters)" % API_MAX_MD)
        return 0
    p = digest_dir() / "digests" / (day + ".md")
    try:
        sys.stdout.write(p.read_text())
    except OSError:
        print("error-digest: no digest for %s" % day, file=sys.stderr)
        return 1
    return 0


def cmd_days(argv):
    if _use_remote():
        status, obj = fetch_remote({})
        if status != 200:
            print("error-digest: %s" % _remote_why(status, obj), file=sys.stderr)
            return 1
    else:
        status, obj = api_read("")
    rows = obj.get("days") or []
    if not rows:
        print("no digests yet")
    for r in rows:
        print("%s  for you %d, for Claude %d, held %d, message %s, model %s" % (
            r.get("day"), r.get("owner") or 0, r.get("claude") or 0, r.get("held") or 0,
            r.get("notify") or "-", r.get("model") or "-"))
    return 0


# ---------------------------------------------------------------------------------------------------- read from elsewhere
# The digest dir lives on the queue's machine (the Unraid container). The operator's Claude sessions run on the Mac, so
# the queue API serves the stored digests at GET /api/error-digest[?day=YYYY-MM-DD]: a private read (the queue token,
# like /api/state), read-only, and the day is checked against a strict pattern before any path is built.
API_DAYS = 14
API_MAX_MD = 512 * 1024
_DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _claude_count(al):
    return len(al.get("claude") or []) + len(al.get("new_for_claude") or []) + (al.get("new_for_claude_more") or 0)


def _read_rec(path):
    try:
        rec = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _status_of(v):
    return v.get("status") if isinstance(v, dict) else None


def api_read(query="", ddir=None):
    """(status, obj) for GET /api/error-digest; `query` is the URL's query string. No day: the newest API_DAYS digests
    with their counts. A day: its Markdown (capped at API_MAX_MD) and its routed items. Only the send and model
    statuses are passed on, never the rest of those records. Never raises."""
    try:
        d = Path(ddir or digest_dir()) / "digests"
        day = (urllib.parse.parse_qs(query or "").get("day") or [""])[0]
        if not day:
            try:
                names = [p.name[:-5] for p in d.iterdir() if p.name.endswith(".json")]
            except OSError:
                names = []
            rows = []
            for k in sorted((n for n in names if _DAY_RE.match(n)), reverse=True)[:API_DAYS]:
                rec = _read_rec(d / (k + ".json"))
                al = rec.get("alerts") if isinstance(rec.get("alerts"), dict) else {}
                rows.append({"day": k, "owner": len(al.get("owner") or []), "claude": _claude_count(al),
                             "held": len(al.get("held") or []), "notify": _status_of(rec.get("notify")),
                             "model": _status_of(rec.get("model"))})
            return 200, {"days": rows}
        if not _DAY_RE.match(day):
            return 400, {"error": "day must be YYYY-MM-DD"}
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            return 400, {"error": "day must be YYYY-MM-DD"}
        rec = _read_rec(d / (day + ".json"))
        try:
            md = (d / (day + ".md")).read_text(errors="replace")
        except OSError:
            md = None
        if md is None and not rec:
            return 404, {"error": "no digest for " + day}
        md = md or ""
        return 200, {"day": day, "md": md[:API_MAX_MD], "md_truncated": len(md) > API_MAX_MD,
                     "alerts": rec.get("alerts") if isinstance(rec.get("alerts"), dict) else {},
                     "notify": _status_of(rec.get("notify")), "model": _status_of(rec.get("model"))}
    except Exception as e:  # noqa: BLE001 - a read route must answer, not crash the handler
        return 500, {"error": type(e).__name__}


def _use_remote():
    """True when the queue (and so the digest dir) is on another machine. Never inside the queue container."""
    if os.environ.get("ERROR_DIGEST_DIR"):
        return False
    try:
        import queue_config
        return bool(queue_config.queue_is_remote())
    except Exception:  # noqa: BLE001 - no queue config: read the local dir
        return False


def fetch_remote(params, timeout=30):
    """(status, obj) from the queue API's /api/error-digest. Status 0 = no answer. The token is never printed."""
    import queue_config
    base, _tok = queue_config.queue_api()
    url = base + "/api/error-digest" + ("?" + urllib.parse.urlencode(params) if params else "")
    hdrs = dict(queue_config.queue_api_headers())
    hdrs["User-Agent"] = "error-digest"
    auth = hdrs.pop("Authorization", None)
    req = urllib.request.Request(url, headers=hdrs)
    if auth:
        req.add_unredirected_header("Authorization", auth)      # never passed on to a redirect's target
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read() or b"{}")
            return r.status, obj if isinstance(obj, dict) else {}
    except urllib.error.HTTPError as e:
        try:
            obj = json.loads(e.read() or b"{}")
        except ValueError:
            obj = {}
        return e.code, obj if isinstance(obj, dict) else {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"error": type(e).__name__}


def _remote_why(status, obj):
    why = str((obj or {}).get("error") or "")[:200]
    if status == 0:
        return "the queue API did not answer (%s)" % (why or "no reply")
    if status == 404 and not why:
        return "the queue API has no /api/error-digest route yet (older image)"
    return "queue API %d%s" % (status, (": " + why) if why else "")


def cmd_grading(argv):
    """One Markdown page over the last N digests: every NEW kind with a box to tick for each one worth knowing."""
    n = int(_arg(argv, "--days", "14"))
    ddir = digest_dir()
    today = dt.date.today()
    out = ["# Error digest grading, last %d days" % n, "",
           "Tick the box for each error you'd want to hear about. Add a line under any digest that missed an error you",
           "cared about. Raw logs: `logq <container> --since 1d --grep error`.", ""]
    for k in range(n, 0, -1):
        day = (today - dt.timedelta(days=k)).isoformat()
        try:
            rec = json.loads((ddir / "digests" / (day + ".json")).read_text())
        except (OSError, ValueError):
            out += ["## %s" % day, "", "_no digest_", ""]
            continue
        meaning = ((rec.get("model") or {}).get("lines") or {})
        out += ["## %s" % day, ""]
        items = [(c, s) for c, i in (rec.get("containers") or {}).items() for s in i["sigs"] if s.get("new")]
        if rec.get("baseline"):
            out.append("_first day on record: nothing marked new_")
        elif not items:
            out.append("_nothing new_")
        for c, s in items:
            out.append("- [ ] **%s** %s ×%d: `%s`%s" % (c, s["level"], s["count"], s["text"].replace("`", "'"),
                                                       ("  \n  Probably: " + meaning[s["id"]]) if meaning.get(s["id"]) else ""))
        out.append("")
    sys.stdout.write("\n".join(out))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    fn = {"run": cmd_run, "backfill": cmd_backfill, "show": cmd_show, "days": cmd_days,
          "grading": cmd_grading, "tick": cmd_tick}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
