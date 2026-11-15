#!/usr/bin/env python3
"""error_digest: one plain-language digest per day of the errors the Unraid docker containers logged.

The operator's OK, 2026-11-15: "automated error handling for docker containers essentially" (scoped to the
Unraid containers). Runs on Unraid (queue container), reads the operator's Loki log stack, never changes a container: no restarts,
no fix jobs (not approved; ask first).

  error_digest.py run [--day YYYY-MM-DD] [--model M | --no-model]   build one day's digest (default: yesterday)
  error_digest.py backfill --days N [--no-model]                     build the last N days, oldest first
  error_digest.py show [--day YYYY-MM-DD]                            print a digest
  error_digest.py grading --days N                                   one page for the operator to mark the errors worth knowing

Per day it writes <dir>/digests/<day>.json and <day>.md (dir: $ERROR_DIGEST_DIR, else <dispatch dir>/error-digest).
The plain digest is always written: per container, error lines grouped into kinds (a "signature": the line with times,
numbers, ids, addresses, URLs and secrets taken out), counts, and which kinds are NEW (not seen in the 14 days before).
Known noise (noise.json in the same dir) is folded into "routine" counts or left out. The local model only adds one
"what it probably means" line per new kind: `run` enqueues one queue job (error-digest-runner.py, Unraid lane) that
fills them in later; when that job fails the digest stays as plain counts.

Loki: $LOKI_URL (or $LOGQ_URL, or queue config logs.loki_url) and a bearer token from $LOKI_TOKEN, the file
$LOKI_TOKEN_FILE / $LOGQ_TOKEN_FILE, or the macOS Keychain item service loki-token account loki. The token is never
printed. Loki already redacts tokens, passwords, API keys and Authorization headers before storing; signatures redact
again (key=value secrets, emails, URLs with their query strings, IPs).
"""
import datetime as dt
import hashlib
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
LOGQL_ERR = r"(?i)\b(error|errors|exception|traceback|fatal|critical|panic|failed|failure|warn|warning)\b"
ERR_WORDS = re.compile(r"\b(error|errors|exception|traceback|fatal|critical|panic|failed|failure)\b", re.I)
WARN_WORDS = re.compile(r"\b(warn|warning)\b", re.I)
# phrases that carry an error word but report that nothing went wrong
BENIGN = re.compile(
    r"\b(?:0|no|zero|without)\s+(?:errors?|failures?|exceptions?)\b"
    r"|\b(?:errors?|failures?|failed|exceptions?|warnings?)(?:_count|_total|_rate)?\s*[=:]\s*(?:0(?:\.0+)?|none|null|false|\[\]|\{\})(?![\w.])"
    r"|\berror_?(?:log|page|handler|reporting|level)\b"
    r"|\bon_?error\b|\bignore_?errors?\b|\bfail_?(?:fast|over|safe)\b|\bnon-?critical\b",
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
    err = pick("error", "err", "exception", "reason")
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


def sig_id(container, sig):
    return hashlib.sha1((container + "\0" + sig).encode()).hexdigest()[:10]


_LV = r"(trace|trc|debug|dbg|info|inf|notice|warn|warning|wrn|err|error|eror|fatal|crit|critical|panic|alert|emerg)"
_TAG_BRACKET = re.compile(r"[\[<(]" + _LV + r"[\]>)]", re.I)
_TAG_KV = re.compile(r"\b(?:level|lvl|severity|loglevel)[\"']?\s*[:=]\s*[\"']?" + _LV + r"\b", re.I)
_TAG_LEAD = re.compile(r"^[\d\-/:.,TZ+ ]*(?:[A-Za-z_][\w.\-]*:?\s+)?(TRACE|TRC|DEBUG|DBG|INFO|INF|NOTICE|WARN|"
                       r"WARNING|WRN|ERROR|ERR|FATAL|CRIT|CRITICAL|PANIC)\b")       # INF/WRN/ERR: zerolog (cloudflared)
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
    if ERR_WORDS.search(s):
        return "error"
    if WARN_WORDS.search(s):
        return "warn"
    return None


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


def summarize_lines(container, rows, rules):
    """rows [(ns, line)] -> container record: kinds with counts, routine/skipped/benign counts."""
    sigs, routine, skipped, benign = {}, {}, 0, 0
    for ns, raw in rows:
        line = clean(raw)
        act = noise_action(rules, container, line)
        if act == "skip":
            skipped += 1
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
            "benign": benign, "sigs": sorted(sigs.values(), key=lambda r: (-r["count"], r["id"]))}


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


def build_day(loki, day, ddir, rules, say=print):
    start, end = day_bounds(day)
    counts = loki.error_counts(start, end)
    seen, hist_days = history(ddir, day)
    rec = {"day": day, "window": [_iso(start), _iso(end)], "generated": _iso(time.time()),
           "history_days": hist_days, "baseline": not hist_days, "containers": {}, "model": None}
    groups = {}
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
        info["capped"] = capped
        info["loki_count"] = sum(counts[n] for n in names)
        if c == ONE_OFF:
            info["names"] = sorted(names)
        prior = seen.get(c, set())
        for s in info["sigs"]:
            s["new"] = bool(hist_days) and s["id"] not in prior
        if info["sigs"] or info["routine"] or info["skipped"]:
            rec["containers"][c] = info
        say("[error-digest] %s %s: %d lines, %d kinds, %d new%s" % (day, c, info["lines"], len(info["sigs"]),
            sum(1 for s in info["sigs"] if s["new"]), " (capped)" if capped else ""))
    return rec


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
    out += ["## By container", "", "| Container | Lines | Kinds | New | Most frequent |", "|---|---|---|---|---|"]
    for c, i in sorted(cs.items(), key=lambda kv: -sum(s["count"] for s in kv[1]["sigs"])):
        lines = sum(s["count"] for s in i["sigs"])
        if not i["sigs"]:
            continue
        t = i["sigs"][0]
        out.append("| %s | %s%s | %d | %d | %d× `%s` |" % (
            c, f"{lines:,}", "+" if i.get("capped") else "", len(i["sigs"]), sum(1 for s in i["sigs"] if s.get("new")),
            t["count"], t["text"][:90].replace("|", "/").replace("`", "'")))
    folded = [(c, i) for c, i in cs.items() if i.get("routine") or i.get("skipped")]
    if folded:
        out += ["", "## Known noise (folded)", ""]
        for c, i in folded:
            bits = []
            if i.get("routine"):
                bits.append("%s routine line%s in %d kind%s" % (f"{i['routine']:,}", "" if i["routine"] == 1 else "s",
                                                                 i["routine_kinds"], "" if i["routine_kinds"] == 1 else "s"))
            if i.get("skipped"):
                bits.append("%s left out (alerted elsewhere)" % f"{i['skipped']:,}")
            out.append("- %s: %s" % (c, ", ".join(bits)))
    return "\n".join(out).rstrip() + "\n"


def _short(t, n=150):
    t = t.replace("`", "'")
    return t if len(t) <= n else t[:n - 1] + "…"


def write_day(ddir, rec):
    d = Path(ddir) / "digests"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in ((rec["day"] + ".json", json.dumps(rec, indent=1)), (rec["day"] + ".md", render(rec))):
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
    return m.group(1)


# ---------------------------------------------------------------------------------------------------- CLI
def _yesterday():
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _arg(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def seed_noise(ddir):
    """noise.json lives in the digest dir so the operator's edits survive image updates; the first run copies the shipped one."""
    dst = Path(ddir) / "noise.json"
    src = Path(HERE) / "error_digest_noise.json"
    if not dst.exists() and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
    return dst


def cmd_run(argv, days=None):
    ddir = digest_dir()
    rules = load_noise(seed_noise(ddir))
    url, tok = loki_settings()
    try:
        loki = Loki(url, tok)
    except LokiError as e:
        print("error-digest: %s" % e, file=sys.stderr)
        return 2
    model = None if "--no-model" in argv else (_arg(argv, "--model") or os.environ.get("ERROR_DIGEST_MODEL")
                                              or MODEL_DEFAULT)
    if days is None:
        days = [_arg(argv, "--day") or _yesterday()]
    for day in days:
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            print("error-digest: bad --day %r (YYYY-MM-DD)" % day, file=sys.stderr)
            return 2
        try:
            rec = build_day(loki, day, ddir, rules)
        except LokiError as e:
            print("error-digest: %s: %s" % (day, e), file=sys.stderr)
            return 1
        want_model = bool(model and model_items(rec))
        if want_model:
            rec["model"] = {"status": "pending", "model": model}
        out = write_day(ddir, rec)
        print("[error-digest] wrote %s" % out)
        if want_model:
            # written BEFORE the job is queued and not rewritten after: the job may finish first, its lines must stay
            jid = enqueue_model_job(ddir, day, model)
            if jid:
                print("[error-digest] model job %s queued" % jid)
            else:
                rec["model"] = {"status": "failed", "model": model, "why": "not queued"}
                write_day(ddir, rec)
    return 0


def cmd_backfill(argv):
    n = int(_arg(argv, "--days", "14"))
    today = dt.date.today()
    days = [(today - dt.timedelta(days=k)).isoformat() for k in range(n, 0, -1)]
    return cmd_run(argv, days=days)


def cmd_show(argv):
    day = _arg(argv, "--day") or _yesterday()
    p = digest_dir() / "digests" / (day + ".md")
    try:
        sys.stdout.write(p.read_text())
    except OSError:
        print("error-digest: no digest for %s" % day, file=sys.stderr)
        return 1
    return 0


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
    fn = {"run": cmd_run, "backfill": cmd_backfill, "show": cmd_show, "grading": cmd_grading}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
