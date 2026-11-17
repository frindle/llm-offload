"""digest_lines: a few count lines from a LAN app for the error digest's daily email (2026-11-16).

First user: resell-tracker, once a day after its last sync ("3 orders synced, 1 pending review"). The 05:07
error-digest run puts each app's lines for that day in their own section of the one daily message, after the
"for you" items. Counts only: the poster must never put order numbers, names, emails or amounts in a line. The check
below is a backstop for the shapes it can see (emails, money, long numbers), not a filter: it cannot spot a name.

  POST /api/digest-lines   {"app": "resell", "day": "YYYY-MM-DD", "lines": ["...", ...], "needs_you": false}
                           -> 200 {"ok": true, "day", "lines": n}; the last POST for an app + day wins ([] clears it)

Rules: app in DIGEST_LINES_APPS (comma list, default "resell"); day = today or up to 2 days back (container clock);
at most 10 lines of at most 200 characters; a line with "@", a currency sign or word (USD, EUR, GBP, dollars), an
amount with cents (12.99, 12,99) or a run of 6+ digits -> 400 "line N looks private".
needs_you (optional, default false) makes the email's subject say the app needs the operator.
Auth: "Authorization: Bearer <QUEUE_DIGEST_TOKEN>" (env, or the file named by QUEUE_DIGEST_TOKEN_FILE); this token
works on this route only, and the route accepts nothing else. No token configured = 503 (never open).
Storage: <error digest dir>/extra/<app>-<day>.json, 0600 in a 0700 dir, deleted after KEEP_DAYS. Nothing is logged.
"""
import datetime as dt
import hmac
import json
import os
import re
import time
from pathlib import Path

ROUTE = "/api/digest-lines"
MAX_BODY = 8 * 1024
MAX_LINES = 10
MAX_LINE = 200
MAX_BACK_DAYS = 2
KEEP_DAYS = 14
MIN_TOKEN_LEN = 24
APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PRIVATE_RE = re.compile(r"[@$\u00a2-\u00a5\u20ac]|\d{6,}|\b\d+[.,]\d{2}\b|\b(?:usd|eur|gbp|dollars?)\b", re.I)
DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")      # fromisoformat alone also takes 20261116 and 2026-W47-1
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
TITLES = {"resell": "Resell"}


def token():
    t = (os.environ.get("QUEUE_DIGEST_TOKEN") or "").strip()
    if not t:
        f = (os.environ.get("QUEUE_DIGEST_TOKEN_FILE") or "").strip()
        if f:
            try:
                t = Path(f).expanduser().read_text().strip()
            except OSError:
                t = ""
    return t if len(t) >= MIN_TOKEN_LEN else ""


def apps():
    return [a.strip() for a in (os.environ.get("DIGEST_LINES_APPS") or "resell").split(",") if APP_RE.match(a.strip())]


def store_dir(ddir=None):
    if ddir is None:
        import error_digest
        ddir = error_digest.digest_dir()
    return Path(ddir) / "extra"


def auth(headers, tok=None):
    """-> None when allowed, else (status, body)."""
    tok = token() if tok is None else tok
    if not tok:
        return 503, {"error": "E_OFF", "detail": "digest lines are off (no QUEUE_DIGEST_TOKEN on the queue)"}
    got = (headers.get("Authorization") or "") if headers is not None else ""
    if got[:7].lower() != "bearer " or not hmac.compare_digest(got[7:].strip().encode(), tok.encode()):
        return 401, {"error": "E_AUTH"}
    return None


def _body(raw, today):
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return None, (400, {"error": "E_BODY", "detail": "body is not JSON"})
    if not isinstance(d, dict):
        return None, (400, {"error": "E_BODY", "detail": "body must be a JSON object"})
    app, day, lines = str(d.get("app") or ""), str(d.get("day") or ""), d.get("lines")
    if app not in apps():
        return None, (400, {"error": "E_BODY", "detail": "unknown app"})
    try:
        dd = dt.date.fromisoformat(day) if DAY_RE.match(day) else None
    except ValueError:
        dd = None
    if dd is None:
        return None, (400, {"error": "E_BODY", "detail": "day: YYYY-MM-DD"})
    if not (today - dt.timedelta(days=MAX_BACK_DAYS) <= dd <= today):
        return None, (400, {"error": "E_BODY", "detail": "day: today or up to %d days back" % MAX_BACK_DAYS})
    if not isinstance(lines, list) or len(lines) > MAX_LINES or not all(isinstance(x, str) for x in lines):
        return None, (400, {"error": "E_BODY", "detail": "lines: a list of at most %d strings" % MAX_LINES})
    clean = []
    for i, x in enumerate(lines, 1):
        x = CTRL_RE.sub(" ", x).strip()
        if len(x) > MAX_LINE:
            return None, (400, {"error": "E_BODY", "detail": "line %d is over %d characters" % (i, MAX_LINE)})
        if PRIVATE_RE.search(x):
            return None, (400, {"error": "E_PRIVATE", "detail": "line %d looks private" % i})
        if x:
            clean.append(x)
    needs = d.get("needs_you", False)
    if not isinstance(needs, bool):
        return None, (400, {"error": "E_BODY", "detail": "needs_you: true or false"})
    return {"app": app, "day": dd.isoformat(), "lines": clean, "needs_you": needs}, None


def sweep(base, now=None, keep_days=KEEP_DAYS):
    now = time.time() if now is None else now
    try:
        for p in Path(base).glob("*.json"):
            try:
                if now - p.stat().st_mtime > keep_days * 86400:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def save(base, req, now=None):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    rec = dict(req, at=dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%SZ"))
    path = base / ("%s-%s.json" % (req["app"], req["day"]))
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return path


def read_day(day, ddir=None):
    """[{app, title, lines, needs_you}] for one day, in DIGEST_LINES_APPS order; apps with no lines are left out.
    Never raises; one app's bad file never costs the others theirs."""
    out = []
    try:
        base = store_dir(ddir)
        names = apps()
    except Exception:  # noqa: BLE001 - the digest must go out without them
        return out
    for app in names:
        try:
            d = json.loads((base / ("%s-%s.json" % (app, day))).read_text())
            lines = [str(x)[:MAX_LINE] for x in (d.get("lines") or [])[:MAX_LINES] if isinstance(x, str) and x.strip()]
            if lines:
                out.append({"app": app, "title": TITLES.get(app, app), "lines": lines,
                            "needs_you": d.get("needs_you") is True})
        except Exception:  # noqa: BLE001 - a missing or bad file: that app has no section
            continue
    return out


def handle(method, path, headers, body=b"", ddir=None, today=None):
    """The whole route: -> (status, obj)."""
    denied = auth(headers)
    if denied:
        return denied
    if path.split("?", 1)[0].rstrip("/") != ROUTE:
        return 404, {"error": "E_NOT_FOUND"}
    if method != "POST":
        return 405, {"error": "E_METHOD"}
    if len(body or b"") > MAX_BODY:
        return 413, {"error": "E_SIZE", "detail": "body over %d bytes" % MAX_BODY}
    req, err = _body(body, today or dt.date.today())
    if err:
        return err
    base = store_dir(ddir)
    sweep(base)
    save(base, req)
    return 200, {"ok": True, "day": req["day"], "lines": len(req["lines"])}
