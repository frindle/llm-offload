"""Board: one card per lane (a thread of work), so every open thread and everything waiting on the owner is on one page.

Lanes write with `board set` on the Mac (~/bin/board) through the queue API; the owner reads GET /board.
  GET  /board                  the page
  GET  /api/board              {"ok", "now", "lanes": [...]}, open lanes first, then done ones from the last 14 days
  GET  /api/board/log?lane=X   that lane's last updates, newest first (?limit=, max 200)
  POST /api/board/set          {"lane", "project"?, "item"?, "status"?, "next"?, "needs"?, "report"?, "by"?}
  POST /api/board/done         {"lane", "report"?, "by"?}
POSTs go through the queue's write auth (ollama-queue-api.py) and must be JSON with X-Requested-With: board, so a
cross-site form cannot post. A field left out keeps its value; "" clears it. Setting `needs` without a status makes
the lane needs-you; clearing it moves a needs-you lane back to active.
Storage: board.json and board-log.jsonl in BOARD_DIR (default QEL_DIR, else ~/.ollama-dispatch). Writes take an
fcntl lock, rewrite board.json atomically, and append the change to the log (rotated at 1 MB, one old copy)."""
import fcntl
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

STATUSES = ("active", "waiting", "needs-you", "blocked", "done")
CAPS = {"project": 80, "item": 300, "next": 400, "needs": 800, "report": 4000, "by": 60}
LANE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
MAX_OPEN = 40
MAX_BODY = 16 * 1024
DONE_KEEP_S = 14 * 86400
LOG_ROTATE = 1024 * 1024
LOG_MAX = 200
_mu = threading.Lock()


def board_dir():
    return Path(os.environ.get("BOARD_DIR") or os.environ.get("QEL_DIR") or os.path.expanduser("~/.ollama-dispatch"))


def _paths():
    d = board_dir()
    return d / "board.json", d / "board-log.jsonl", d / "board.json.lock"


def _clean(s, cap):
    s = "" if s is None else str(s)
    s = "".join(ch for ch in s if ch.isprintable() or ch == "\n").strip()
    return s[:cap]


def _read(path):
    try:
        data = json.loads(path.read_text())
        lanes = data.get("lanes") if isinstance(data, dict) else None
        return {k: v for k, v in (lanes or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _write(path, lanes):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"lanes": lanes}, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _log(path, entry):
    try:
        if path.stat().st_size > LOG_ROTATE:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    with open(path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _prune(lanes, now):
    return {k: v for k, v in lanes.items()
            if v.get("status") != "done" or now - float(v.get("closed") or v.get("updated") or 0) < DONE_KEEP_S}


def lanes_view(now=None):
    now = time.time() if now is None else now
    board, _, _ = _paths()
    lanes = list(_prune(_read(board), now).values())
    lanes.sort(key=lambda v: (v.get("status") == "done", not v.get("needs"), -float(v.get("updated") or 0)))
    return lanes


def _update(lane, fields, by, now, close=False):
    """-> (status, obj). Applies one change under the lock."""
    board, logp, lockp = _paths()
    board.parent.mkdir(parents=True, exist_ok=True)
    with _mu, open(lockp, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        lanes = _prune(_read(board), now)
        cur = dict(lanes.get(lane) or {})
        if close and not cur:
            return 404, {"ok": False, "error": "no such lane"}
        is_new = not cur or cur.get("status") == "done"
        if is_new and not close and sum(1 for v in lanes.values() if v.get("status") != "done") >= MAX_OPEN:
            return 409, {"ok": False, "error": f"{MAX_OPEN} lanes already open; close one first"}
        if is_new and not close:
            cur = {"lane": lane, "created": now}
        changed = {}
        for k, v in fields.items():
            if cur.get(k, "") != v:
                changed[k] = v
                cur[k] = v
        if close:
            if cur.get("status") != "done":
                changed["status"] = "done"
            cur["status"], cur["closed"] = "done", now
            if cur.get("needs"):
                changed["needs"] = ""
                cur["needs"] = ""
        elif "status" not in fields:
            if cur.get("needs") and cur.get("status") in (None, "", "active", "waiting", "done"):
                cur["status"] = changed["status"] = "needs-you"
            elif not cur.get("needs") and cur.get("status") in (None, "", "needs-you", "done"):
                cur["status"] = changed["status"] = "active"
        cur["updated"], cur["by"] = now, by or cur.get("by", "")
        lanes[lane] = cur
        _write(board, lanes)
        _log(logp, {"ts": now, "lane": lane, "by": by, "changes": changed})
    return 200, {"ok": True, "lane": cur, "changed": sorted(changed)}


def handle(method, path, headers, body, now=None):
    """-> (status, json-able). Auth is the caller's job; this checks shape and content only."""
    now = time.time() if now is None else now
    u = urllib.parse.urlsplit(path)
    route = u.path.rstrip("/")
    if method == "GET" and route == "/api/board":
        return 200, {"ok": True, "now": now, "statuses": list(STATUSES), "lanes": lanes_view(now)}
    if method == "GET" and route == "/api/board/log":
        q = urllib.parse.parse_qs(u.query)
        lane = (q.get("lane") or [""])[0]
        try:
            limit = max(1, min(LOG_MAX, int((q.get("limit") or [50])[0])))
        except ValueError:
            return 400, {"ok": False, "error": "limit must be a number"}
        _, logp, _ = _paths()
        rows = []
        for p in (logp.with_name(logp.name + ".1"), logp):
            try:
                lines = p.read_text().splitlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict) and (not lane or e.get("lane") == lane):
                    rows.append(e)
        rows.sort(key=lambda e: -float(e.get("ts") or 0))
        return 200, {"ok": True, "log": rows[:limit]}
    if method != "POST" or route not in ("/api/board/set", "/api/board/done"):
        return 404, {"ok": False, "error": "not found"}
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, {"ok": False, "error": "Content-Type must be application/json"}
    if headers.get("X-Requested-With") != "board":
        return 403, {"ok": False, "error": "missing X-Requested-With: board"}
    if len(body) > MAX_BODY:
        return 413, {"ok": False, "error": f"body too large (max {MAX_BODY} bytes)"}
    try:
        data = json.loads(body.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "body must be valid JSON"}
    if not isinstance(data, dict):
        return 400, {"ok": False, "error": "body must be a JSON object"}
    lane = data.get("lane")
    if not isinstance(lane, str) or not LANE_RE.match(lane):
        return 400, {"ok": False, "error": "lane must match " + LANE_RE.pattern}
    allowed = {"lane", "by", "report"} | ({"project", "item", "status", "next", "needs"} if route.endswith("/set") else set())
    extra = set(data) - allowed
    if extra:
        return 400, {"ok": False, "error": "unknown fields: " + ", ".join(sorted(extra))}
    fields = {}
    for k in ("project", "item", "next", "needs", "report"):
        if k in data:
            if data[k] is not None and not isinstance(data[k], str):
                return 400, {"ok": False, "error": f"{k} must be a string"}
            fields[k] = _clean(data[k], CAPS[k])
    if "status" in data:
        if data["status"] not in STATUSES or data["status"] == "done":
            return 400, {"ok": False, "error": "status must be one of " + ", ".join(s for s in STATUSES if s != "done")
                         + " (close a lane with /api/board/done)"}
        fields["status"] = data["status"]
    by = _clean(data.get("by"), CAPS["by"]) if isinstance(data.get("by"), str) else ""
    return _update(lane, fields, by, now, close=route.endswith("/done"))


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Work Board</title>
<style>
  :root {
    --bg: #f5f6f8; --surface: #ffffff; --surface-2: #eef1f4; --line: #dde2e8;
    --fg: #17202b; --muted: #5b6676; --accent: #0e6a75;
    --run: #18794a; --run-bg: #e2f3e9; --warn: #8f5600; --warn-bg: #fcefd8;
    --bad: #b42318; --bad-bg: #fde7e4; --info: #38598a; --info-bg: #e6edf7;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
    --bg: #0f1318; --surface: #161b22; --surface-2: #1c232c; --line: #2a323d;
    --fg: #e5e9ef; --muted: #97a2b0; --accent: #5bbcc8;
    --run: #56c690; --run-bg: #11301f; --warn: #f0b452; --warn-bg: #33260e;
    --bad: #ff8172; --bad-bg: #3b1714; --info: #93b2e8; --info-bg: #1a2639;
    color-scheme: dark; } }
  :root[data-theme="dark"] {
    --bg: #0f1318; --surface: #161b22; --surface-2: #1c232c; --line: #2a323d;
    --fg: #e5e9ef; --muted: #97a2b0; --accent: #5bbcc8;
    --run: #56c690; --run-bg: #11301f; --warn: #f0b452; --warn-bg: #33260e;
    --bad: #ff8172; --bad-bg: #3b1714; --info: #93b2e8; --info-bg: #1a2639;
    color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 var(--sans); }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 20px 16px 56px; display: grid; gap: 22px; }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 1.25rem; margin: 0; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 10px; }
  a { color: var(--accent); }
  .upd { margin-left: auto; color: var(--muted); font-size: .85rem; }
  .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 330px), 1fr)); }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
          display: grid; gap: 6px; min-width: 0; }
  .card.needs { border-color: var(--warn); }
  .top { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .lane { font-weight: 600; overflow-wrap: anywhere; }
  .proj { color: var(--muted); font-size: .85rem; overflow-wrap: anywhere; }
  .age { margin-left: auto; color: var(--muted); font-size: .8rem; white-space: nowrap; }
  .age.stale { color: var(--bad); }
  .pill { font-size: .72rem; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }
  .s-active { background: var(--run-bg); color: var(--run); }
  .s-waiting { background: var(--info-bg); color: var(--info); }
  .s-needs-you { background: var(--warn-bg); color: var(--warn); }
  .s-blocked { background: var(--bad-bg); color: var(--bad); }
  .s-done { background: var(--surface-2); color: var(--muted); }
  .row { display: grid; grid-template-columns: 3.2em 1fr; gap: 8px; }
  .row b { color: var(--muted); font-weight: 500; font-size: .85rem; }
  .row span { overflow-wrap: anywhere; white-space: pre-wrap; }
  .ask { background: var(--warn-bg); color: var(--fg); border-radius: 6px; padding: 8px 10px; white-space: pre-wrap;
         overflow-wrap: anywhere; }
  details { font-size: .9rem; }
  summary { cursor: pointer; color: var(--accent); }
  .report { white-space: pre-wrap; overflow-wrap: anywhere; margin: 6px 0 0; }
  .log { margin: 6px 0 0; padding: 0; list-style: none; display: grid; gap: 4px; font-size: .82rem; color: var(--muted); }
  .log li { overflow-wrap: anywhere; }
  .empty { color: var(--muted); }
  .err { color: var(--bad); }
</style></head>
<body>
<div class="wrap">
<header>
  <h1>Work Board</h1>
  <a href="/">Queue &rarr;</a>
  <span class="upd" id="upd">loading&hellip;</span>
</header>
<section id="needsSec" hidden><h2>Needs you</h2><div class="grid" id="needs"></div></section>
<section><h2>Lanes</h2><div class="grid" id="open"></div></section>
<section id="doneSec" hidden><details><summary>Done in the last 14 days (<span id="doneN">0</span>)</summary>
  <div class="grid" id="done" style="margin-top:10px"></div></details></section>
</div>
<script>
const $ = id => document.getElementById(id);
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function ago(s) {
  s = Math.max(0, s);
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 129600) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}
function row(label, text) {
  const r = el('div', 'row'); r.append(el('b', null, label), el('span', null, text)); return r;
}
function card(l, now) {
  const c = el('div', 'card' + (l.needs ? ' needs' : ''));
  const top = el('div', 'top');
  top.append(el('span', 'lane', l.lane), el('span', 'pill s-' + (l.status || 'active'), (l.status || 'active').replace('-', ' ')));
  const idle = now - (l.updated || 0);
  top.append(el('span', 'age' + (l.status !== 'done' && idle > 86400 ? ' stale' : ''), ago(idle)));
  c.append(top);
  if (l.project) c.append(el('div', 'proj', l.project + (l.by ? ' · ' + l.by : '')));
  if (l.needs) c.append(el('div', 'ask', l.needs));
  if (l.item) c.append(row('Now', l.item));
  if (l.next) c.append(row('Next', l.next));
  const d = el('details');
  d.append(el('summary', null, l.report ? 'Report and history' : 'History'));
  if (l.report) d.append(el('div', 'report', l.report));
  const ul = el('ul', 'log'); d.append(ul);
  d.addEventListener('toggle', async () => {
    if (!d.open || ul.dataset.loaded) return;
    ul.dataset.loaded = 1;
    try {
      const r = await fetch('/api/board/log?limit=30&lane=' + encodeURIComponent(l.lane));
      const j = await r.json();
      for (const e of j.log || []) {
        const ch = e.changes || {};
        const bits = Object.keys(ch).filter(k => k !== 'report').map(k => k + ': ' + (ch[k] === '' ? '(cleared)' : ch[k]));
        if ('report' in ch) bits.push('report updated');
        ul.append(el('li', null, ago(now - e.ts) + (e.by ? ' · ' + e.by : '') + ' — ' + (bits.join('; ') || 'touched')));
      }
      if (!ul.children.length) ul.append(el('li', null, 'no history'));
    } catch (e) { ul.append(el('li', 'err', 'could not load history')); }
  });
  c.append(d);
  return c;
}
function fill(id, items, now) {
  const g = $(id); g.replaceChildren();
  for (const l of items) g.append(card(l, now));
  return items.length;
}
async function load() {
  try {
    const r = await fetch('/api/board', {cache: 'no-store'});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'error');
    const now = j.now, lanes = j.lanes || [];
    const open = lanes.filter(l => l.status !== 'done');
    const needs = open.filter(l => l.needs), rest = open.filter(l => !l.needs);
    $('needsSec').hidden = !fill('needs', needs, now);
    if (!fill('open', rest, now)) $('open').append(el('div', 'empty', needs.length ? 'Nothing else open.' : 'No open lanes.'));
    const done = lanes.filter(l => l.status === 'done');
    $('doneN').textContent = done.length; $('doneSec').hidden = !done.length;
    fill('done', done, now);
    $('upd').textContent = 'updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    $('upd').className = 'upd';
  } catch (e) {
    $('upd').textContent = 'could not load: ' + e.message; $('upd').className = 'upd err';
  }
}
load();
// A card's open history would close on a redraw, so wait until it is closed again.
setInterval(() => { if (!document.hidden && !document.querySelector('.card details[open]')) load(); }, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
</script>
</body></html>
"""
