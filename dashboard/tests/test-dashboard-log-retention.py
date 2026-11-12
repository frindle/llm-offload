#!/usr/bin/env python3
"""Log retention panel (ollama-queue-api.py): API + page behaviour.
Server: strict body-shape validation, size cap, CSRF headers, save/pin/unpin round trip against a
temp HOME + temp config (the real ~/.ollama-dispatch is never touched), preview never writes, audit-log
summary, real HTTP routing. Page: runs the panel's JS under node against a fake DOM + fetch:
three levels with 'inherit', debounced + race-safe preview, save success / failure / HANG (aborted),
no optimistic 'saved', failed/hung/non-ok refresh -> visible 'unavailable', pin add / remove, escaping.
Run: python3 tests/test-dashboard-log-retention.py [--src PATH] [--mutants]"""
import importlib.util, json, os, re, subprocess, sys, tempfile, threading, urllib.request, urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []
DEFAULT_SRC = HERE.parent / "src" / "ollama-queue-api.py"

MUTANTS = [
    # --- server
    ("unknown body fields allowed", "    if extra:\n        return None, f\"{what} has unknown", "    if False:\n        return None, f\"{what} has unknown"),
    ("CSRF header not required", 'if headers.get("X-Requested-With") != "dashboard":', "if False:"),
    ("content-type not required", 'if method == "POST" and ctype != "application/json":', "if False:"),
    ("body size uncapped", "if len(body) > LR_MAX_BODY:\n            return None,", "if False:\n            return None,"),
    ("non-object body accepted", 'if not isinstance(data, dict):\n            return None, (400, {"ok": False, "error": "body must be a JSON object"})', "if False:\n            return None, (400, {})"),
    ("save drops server-owned pins", "    cfg = dict(cfg)\n    cfg.update(levels)", "    cfg = dict(levels)"),
    ("duplicate pin allowed", 'if any(p.get("id") == pid for p in pins):', "if False:"),
    ("pin id unchecked", "if not LR_PIN_ID_RE.match(pid):\n            return 400", "if False:\n            return 400"),
    ("empty reason allowed", "if not reason or len(reason) > LR_MAX_REASON:", "if False:"),
    ("unpin of absent id is ok", "if len(keep) == len(pins):", "if False:"),
    ("preview saves", '        res = _lr_preview_cached(cfg)\n        if res.get("errors"):\n            return 503', '        C.save(cfg, who="x")\n        res = _lr_preview_cached(cfg)\n        if res.get("errors"):\n            return 503'),
    ("pin type always bundle", 'ptype = "job" if LR_JOB_ID_RE.match(pid) else "bundle"', 'ptype = "bundle"'),
    ("audit: dry-run counted as last apply", 'row["mode"] == "apply":', 'row["mode"] == "dry-run":'),
    ("DELETE route unwired", 'return self._log_retention("DELETE")', 'return self._text("not found", 404)'),
    ("POST route unwired", 'return self._log_retention("POST")', 'return self._text("not found", 404)'),
    ("pin id unescaped in query", "pid = ids[0]\n", "pid = ids[0] + 'x'\n"),
    # --- page
    ("panel opens expanded", '<details id="logRetention">', '<details id="logRetention" open>'),
    ("04:17 note missing", "<strong>daily at 04:17</strong>", "<strong>daily</strong>"),
    ("save failure shown as saved", "lrSetMsg('NOT saved: ' + (e && e.name", "lrSetMsg('saved ' + (e && e.name"),
    ("no abort timeout", "const timer = setTimeout(() => ac.abort(), LR_TIMEOUT_MS);", "const timer = setTimeout(() => {}, LR_TIMEOUT_MS);"),
    ("non-ok response accepted", "if (!r || !r.ok) throw new Error((d && d.error)", "if (!r) throw new Error((d && d.error)"),
    ("unavailable keeps old preview", "['All', 'Failures', 'Successful'].forEach(k => { lrEl('lrPrev' + k).textContent = '\u2014'; });", ""),
    ("refresh failure keeps last state", "  } catch (e) {\n    lrRenderUnavailable();", "  } catch (e) {\n    /* keep last */"),
    ("stale preview answer applied", "    if (seq !== lrPrevSeq) return;   // a newer edit superseded this answer\n", ""),
    ("save shows typed value not server value", "lrRenderState(Object.assign({}, d, {preview: null}));", "lrRenderState({config: inp.cfg});"),
    ("no inherit placeholder", "lrEl('lrFailures').placeholder = 'inherit (' + c.all + ')';", "lrEl('lrFailures').placeholder = '';"),
    ("null failures rendered as 'null'", "lrEl('lrFailures').value = c.failures == null ? '' : String(c.failures);", "lrEl('lrFailures').value = String(c.failures);"),
    ("double save allowed", "async function lrSave() {\n  if (lrSaving) return;\n", "async function lrSave() {\n"),
    ("client accepts junk numbers", "if (!/^\\d{1,3}$/.test(s)) return {err:", "if (false) return {err:"),
    ("pin reason unescaped", "escapeHtml(p.why)", "p.why"),
    ("unpin id not encoded", "encodeURIComponent(id)", "id"),
    ("CSRF header not sent", "'X-Requested-With': 'dashboard'", "'X-Requested-With': 'x'"),
    ("refresh guard never released", "  } finally { lrBusy = false; }", "  } finally { }"),
    ("preview not debounced", "lrPrevTimer = setTimeout(lrRunPreview, LR_DEBOUNCE_MS);", "lrRunPreview();"),
    ("save button unwired", "lrEl('lrSave').addEventListener('click', lrSave);", ""),
    ("unpin click unwired", "if (b) lrUnpin(b.getAttribute('data-id'));", ""),
]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load_module(src):
    home = tempfile.mkdtemp(prefix="dash-lr-")
    os.environ["HOME"] = home
    os.environ["LOG_ROTATE_HOME"] = home
    os.environ.pop("LOG_RETENTION_CONFIG", None)
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    sys.argv = [str(src)]
    ld = SourceFileLoader("qapi_lr", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_lr", ld))
    ld.exec_module(m)
    return m, Path(home)


JS = r"""
const els = {};
function mk(id) { return els[id] || (els[id] = {id, value: '', textContent: '', innerHTML: '', placeholder: '', disabled: false,
  open: false, style: {}, addEventListener() {}, getAttribute() { return null; }}); }
globalThis.document = {getElementById: mk};
const escapeHtml = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const calls = [];
let srv = null;
globalThis.fetch = async (url, opts) => {
  opts = opts || {};
  const call = {url, method: opts.method || 'GET', headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body) : undefined};
  calls.push(call);
  const out = await srv(call, opts);
  if (out === 'hang') return new Promise((_, rej) => opts.signal.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; rej(e); }));
  if (out === 'reject') throw new Error('net down');
  return {ok: out.status < 400, status: out.status, json: async () => { if (out.raw !== undefined) throw new Error('bad json'); return out.body; }};
};
%PARTS%
LR_TIMEOUT_MS = 60; LR_DEBOUNCE_MS = 20;
const lv = (c, b) => ({count: c, bytes: b * 1048576});
const preview = (a, f, s) => ({levels: {all: lv(a, a), failures: lv(f, f), successful: lv(s, s), ledger: lv(0, 0)},
  total: lv(a + f + s, a + f + s), protected: lv(1, 1), pinned: lv(0, 0), errors: []});
const cfg = (o) => Object.assign({version: 1, all: 30, failures: null, successful: 14, ledger_days: 365, pins: []}, o || {});
const last = {last_run: {ts: '2026-10-13T11:17:03Z', mode: 'apply', deleted: 12, freed_bytes: 5 * 1048576},
  last_apply: {ts: '2026-10-13T11:17:03Z', mode: 'apply', deleted: 12, freed_bytes: 5 * 1048576}, schedule: 'daily at 04:17'};
const state = (c) => ({config: c, errors: [], validation: [], preview: preview(12, 3, 1), last_rotation: last});
const ok = body => ({status: 200, body});
const bad = (status, error) => ({status, body: {ok: false, error}});
const posts = () => calls.filter(c => c.method !== 'GET');
const out = {};
(async () => {
  // 1. render: three levels, inherit placeholder, preview, last rotation, escaping
  srv = async () => ok(state(cfg({pins: [{type: 'job', id: 'abc', who: 'owner', when: '2026-10-13T00:00:00Z', why: '<b>x</b>'}]})));
  await refreshLogRetention();
  out.r_all = mk('lrAll').value; out.r_fail = mk('lrFailures').value; out.r_succ = mk('lrSuccessful').value;
  out.r_ph_f = mk('lrFailures').placeholder; out.r_ph_s = mk('lrSuccessful').placeholder;
  out.r_prev = mk('lrPrevAll').textContent + '|' + mk('lrPrevFailures').textContent + '|' + mk('lrPrevSuccessful').textContent;
  out.r_pins = mk('lrPins').innerHTML;
  out.r_last = mk('lrLast').innerHTML;
  out.r_save_enabled = mk('lrSave').disabled === false;

  // 2. preview: debounced into one call, carries only the three levels + CSRF header
  calls.length = 0;
  srv = async (c) => ok({ok: true, preview: preview(7, 2, 1)});
  mk('lrAll').value = '20'; mk('lrFailures').value = '90'; mk('lrSuccessful').value = '';
  lrSchedulePreview(); lrSchedulePreview(); lrSchedulePreview();
  await sleep(120);
  out.p_n = calls.length; out.p_url = calls[0] && calls[0].url; out.p_body = calls[0] && calls[0].body;
  out.p_hdr = calls[0] && calls[0].headers['X-Requested-With'];
  out.p_text = mk('lrPrevAll').textContent + '|' + mk('lrPrevTotal').textContent;
  // out-of-order answers: a slow older answer must not overwrite the newer one
  srv = async (c) => { if (c.body.config.all === 1) { await sleep(60); return ok({ok: true, preview: preview(111, 0, 0)}); }
                       return ok({ok: true, preview: preview(222, 0, 0)}); };
  mk('lrAll').value = '1'; const slow = lrRunPreview();
  await sleep(5); mk('lrAll').value = '2'; await lrRunPreview(); await slow;
  out.p_race = mk('lrPrevAll').textContent;
  // preview failure: numbers cleared + visible message, never the old numbers
  srv = async () => bad(500, 'boom');
  mk('lrAll').value = '3'; await lrRunPreview();
  out.pf_text = mk('lrPrevAll').textContent; out.pf_msg = mk('lrMsg').textContent;

  // 3. save success shows what the SERVER stored (server answers 99 for typed 40)
  calls.length = 0;
  srv = async (c) => ok({ok: true, config: cfg({all: 99, successful: null}), last_rotation: last, schedule: 'daily at 04:17'});
  mk('lrAll').value = '40'; mk('lrFailures').value = ''; mk('lrSuccessful').value = '';
  await lrSave();
  out.s_post = JSON.stringify(calls[0].body); out.s_method = calls[0].method; out.s_hdr = calls[0].headers['X-Requested-With'];
  out.s_val = mk('lrAll').value; out.s_msg = mk('lrMsg').textContent; out.s_color = mk('lrMsg').style.color;
  await sleep(60);

  // 4. save failure: NOT saved, red
  srv = async () => bad(400, 'all must be an integer 1..365');
  mk('lrAll').value = '40'; await lrSave();
  out.f_msg = mk('lrMsg').textContent; out.f_color = mk('lrMsg').style.color;
  // network reject
  srv = async () => 'reject'; await lrSave();
  out.f2_msg = mk('lrMsg').textContent;

  // 5. save hang: aborted at the timeout, guard released afterwards
  srv = async () => 'hang'; await lrSave();
  out.h_msg = mk('lrMsg').textContent;
  calls.length = 0; srv = async () => ok({ok: true, config: cfg(), last_rotation: last}); await lrSave();
  out.h_after = calls.length;

  // 6. client-side junk never leaves the browser
  calls.length = 0;
  for (const v of ['12abc', '0', '366', '', '1.5', '-3']) { mk('lrAll').value = v; await lrSave(); }
  mk('lrAll').value = '30'; mk('lrFailures').value = 'x'; await lrSave(); mk('lrFailures').value = '';
  out.j_calls = calls.length; out.j_msg = mk('lrMsg').textContent;

  // 7. double click saves once
  calls.length = 0; mk('lrAll').value = '30';
  srv = async () => { await sleep(20); return ok({ok: true, config: cfg(), last_rotation: last}); };
  await Promise.all([lrSave(), lrSave()]);
  out.d_posts = calls.filter(c => c.method === 'POST' && c.url === '/api/log-retention').length;

  // 8. refresh failure modes replace the panel with 'unavailable'; recovery re-enables
  for (const mode of ['reject', 'hang', 'notok', 'badjson']) {
    srv = async () => ok(state(cfg()));
    await refreshLogRetention();
    srv = async () => mode === 'notok' ? bad(502, 'bad gateway') : mode === 'badjson' ? {status: 200, raw: true} : mode;
    await refreshLogRetention();
    out['u_' + mode] = /unavailable \(last seen \d+s ago\)/.test(mk('lrSummary').textContent)
      && mk('lrPins').innerHTML.includes('unavailable') && mk('lrPrevAll').textContent === '\u2014'
      && mk('lrSave').disabled === true && mk('lrPinAdd').disabled === true && mk('lrLast').textContent === '';
    out['u_' + mode + '_msg'] = mk('lrMsg').style.color === '#c00';
  }
  srv = async () => ok(state(cfg()));
  await refreshLogRetention();
  out.u_recover = mk('lrSave').disabled === false && !mk('lrPins').innerHTML.includes('unavailable');

  // 9. pins: add, duplicate refused, empty refused, unpin
  calls.length = 0;
  const pinned = cfg({pins: [{type: 'bundle', id: 'my-bundle', who: 'dashboard', when: '2026-10-13T01:02:03Z', why: 'debug'}]});
  srv = async () => ok({ok: true, config: pinned, last_rotation: last});
  mk('lrPinId').value = 'my-bundle'; mk('lrPinWhy').value = 'debug';
  await lrAddPin();
  out.a_body = JSON.stringify(calls[0].body); out.a_url = calls[0].url;
  out.a_pins = mk('lrPins').innerHTML.includes('my-bundle'); out.a_cleared = mk('lrPinId').value === '' && mk('lrPinWhy').value === '';
  calls.length = 0; mk('lrPinId').value = 'x'; mk('lrPinWhy').value = '   '; await lrAddPin();
  out.a_empty_calls = calls.length;
  srv = async () => bad(409, 'my-bundle is already pinned');
  mk('lrPinId').value = 'my-bundle'; mk('lrPinWhy').value = 'again'; await lrAddPin();
  out.a_dup_msg = mk('lrMsg').textContent; out.a_dup_kept = mk('lrPinId').value === 'my-bundle';
  out.a_dup_pins_still = mk('lrPins').innerHTML.includes('my-bundle');
  calls.length = 0;
  srv = async () => ok({ok: true, config: cfg(), last_rotation: last});
  await lrUnpin('x&y');
  out.un_call = calls[0].method + ' ' + calls[0].url; out.un_hdr = calls[0].headers['X-Requested-With'];
  out.un_pins = mk('lrPins').innerHTML.includes('no pins');
  srv = async () => bad(404, 'x is not pinned'); await lrUnpin('x');
  out.un_fail = mk('lrMsg').textContent;
  console.log(JSON.stringify(out));
})();
"""


def js_checks(html):
    m = re.search(r"^// ---- Settings: Log retention.*?^(?=\['lrAll', 'lrFailures')", html, re.S | re.M)
    check("log retention JS block present", bool(m), True)
    if not m:
        return
    f = Path(tempfile.mkdtemp()) / "t.js"
    f.write_text(JS.replace("%PARTS%", m.group(0)))
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        check("node harness ran to completion", r.stdout[-300:] + r.stderr[-400:], "completed")
        return
    check("All shows its value", o["r_all"], "30")
    check("Failures empty when inheriting", o["r_fail"], "")
    check("Successful shows override", o["r_succ"], "14")
    check("inherit placeholders name the inherited days", (o["r_ph_f"], o["r_ph_s"]), ("inherit (30)", "inherit (30)"))
    check("per-level preview count + MB", o["r_prev"], "12 logs, 12.0 MB|3 logs, 3.0 MB|1 logs, 1.0 MB")
    check("pin text is escaped", "&lt;b&gt;x&lt;/b&gt;" in o["r_pins"] and "<b>x" not in o["r_pins"], True)
    check("pin row has an unpin button with its id", 'class="lr-unpin" data-id="abc"' in o["r_pins"], True)
    check("last rotation shows when + bytes freed + schedule", all(t in o["r_last"] for t in ("2026-10-13T11:17:03Z", "freed 5.0 MB", "04:17")), True)
    check("save enabled when loaded", o["r_save_enabled"], True)
    check("preview debounced to ONE call", o["p_n"], 1)
    check("preview posts to /preview", o["p_url"], "/api/log-retention/preview")
    check("preview body is exactly the three levels", o["p_body"], {"config": {"all": 20, "failures": 90, "successful": None}})
    check("preview carries CSRF header", o["p_hdr"], "dashboard")
    check("preview text updates", o["p_text"].startswith("7 logs, 7.0 MB|Total: 10 logs"), True)
    check("slow older preview does not overwrite newer", o["p_race"], "222 logs, 222.0 MB")
    check("preview failure clears numbers", o["pf_text"], "\u2014")
    check("preview failure is visible", o["pf_msg"].startswith("preview unavailable"), True)
    check("save posts the three levels", json.loads(o["s_post"]), {"config": {"all": 40, "failures": None, "successful": None}})
    check("save is a POST with CSRF header", (o["s_method"], o["s_hdr"]), ("POST", "dashboard"))
    check("after save the field shows the SERVER's value", o["s_val"], "99")
    check("save success state", (o["s_msg"], o["s_color"]), ("saved", "#0a0"))
    check("save failure says NOT saved with the server reason", o["f_msg"], "NOT saved: all must be an integer 1..365")
    check("save failure is red", o["f_color"], "#c00")
    check("network failure says NOT saved", o["f2_msg"].startswith("NOT saved"), True)
    check("hung save aborted at timeout", o["h_msg"], "NOT saved: timed out")
    check("save works again after a hang (guard released)", o["h_after"], 1)
    check("junk input never posted", o["j_calls"], 0)
    check("junk input says NOT saved", o["j_msg"].startswith("NOT saved"), True)
    check("double click posts once", o["d_posts"], 1)
    for mode in ("reject", "hang", "notok", "badjson"):
        check(f"refresh {mode}: panel shows unavailable, nothing stale", o["u_" + mode], True)
        check(f"refresh {mode}: red status", o["u_" + mode + "_msg"], True)
    check("recovery re-enables the panel", o["u_recover"], True)
    check("add pin body is ids + reason only", json.loads(o["a_body"]), {"job_or_bundle": "my-bundle", "reason": "debug"})
    check("add pin url", o["a_url"], "/api/log-retention/pin")
    check("pin list re-rendered from server answer, form cleared", (o["a_pins"], o["a_cleared"]), (True, True))
    check("pin with blank reason not sent", o["a_empty_calls"], 0)
    check("duplicate pin shows server error", o["a_dup_msg"], "pin failed: my-bundle is already pinned")
    check("failed pin keeps the form, list unchanged", (o["a_dup_kept"], o["a_dup_pins_still"]), (True, True))
    check("unpin is DELETE with encoded id + CSRF header", (o["un_call"], o["un_hdr"]), ("DELETE /api/log-retention/pin?id=x%26y", "dashboard"))
    check("unpin re-renders the list", o["un_pins"], True)
    check("unpin failure shown", o["un_fail"], "unpin failed: x is not pinned")


def http(port, method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", "X-Requested-With": "dashboard"}
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def api_checks(m, home):
    cfgp = home / ".ollama-dispatch" / "log-retention.json"
    audit = home / ".ollama-dispatch" / "log-rotate.log"
    C = m._lr_mod()
    # fake the (slow, state-dependent) dry run; the real one is exercised once below
    calls = []
    real_preview = C.preview
    C.preview = lambda cfg=None, paths=None: (calls.append(json.loads(json.dumps(cfg))) or
        {"levels": {k: {"count": 1, "bytes": 1048576} for k in ("all", "failures", "successful", "ledger")},
         "total": {"count": 3, "bytes": 3145728}, "protected": {"count": 0, "bytes": 0},
         "pinned": {"count": 0, "bytes": 0}, "errors": []})
    srv = m.ThreadingServer(("127.0.0.1", 0), m.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, d = http(port, "GET", "/api/log-retention", headers={"Content-Type": "text/plain"})
        check("GET defaults: 30 / inherit / inherit", (st, d["config"]["all"], d["config"]["failures"], d["config"]["successful"]), (200, 30, None, None))
        check("GET carries preview, validation, schedule", ("levels" in d["preview"], d["validation"], d["schedule"]), (True, [], "daily at 04:17"))
        check("GET wrote nothing", cfgp.exists(), False)

        # --- preview: no save, uses merged cfg, validates
        st, d = http(port, "POST", "/api/log-retention/preview", {"config": {"all": 10, "failures": 90, "successful": None}})
        check("preview ok", (st, d["ok"], d["preview"]["total"]["count"]), (200, True, 3))
        check("preview did not write config", cfgp.exists(), False)
        check("preview received the candidate levels", (calls[-1]["all"], calls[-1]["failures"], calls[-1]["successful"]), (10, 90, None))
        for name, body in [("out of range high", {"config": {"all": 366, "failures": None, "successful": None}}),
                           ("zero", {"config": {"all": 0, "failures": None, "successful": None}}),
                           ("negative failures", {"config": {"all": 5, "failures": -1, "successful": None}}),
                           ("string days", {"config": {"all": "30", "failures": None, "successful": None}}),
                           ("float days", {"config": {"all": 30.5, "failures": None, "successful": None}}),
                           ("bool days", {"config": {"all": True, "failures": None, "successful": None}}),
                           ("string failures", {"config": {"all": 30, "failures": "7", "successful": None}}),
                           ("unknown field", {"config": {"all": 30, "failures": None, "successful": None, "ledger_days": 30}}),
                           ("pins smuggled", {"config": {"all": 30, "pins": []}}),
                           ("missing all", {"config": {"failures": 5}}),
                           ("config not object", {"config": [1, 2]}),
                           ("config null", {"config": None}),
                           ("extra top-level key", {"config": {"all": 30}, "path": "/etc"}),
                           ("missing config", {})]:
            st, d = http(port, "POST", "/api/log-retention/preview", body)
            check(f"preview rejects {name}", (st, d and d.get("ok")), (400, False))

        # --- body shape / csrf / size
        for name, raw in [("array body", b"[1,2]"), ("string body", b'"x"'), ("number body", b"7"), ("null body", b"null"),
                          ("invalid json", b"{nope"), ("empty body", b""), ("binary junk", b"\xff\xfe\x00")]:
            st, d = http(port, "POST", "/api/log-retention", raw=raw)
            check(f"save rejects {name}", (st, d and d.get("ok")), (400, False))
        st, d = http(port, "POST", "/api/log-retention", raw=b'{"config": {"all": 30}, "pad": "' + b"x" * 20000 + b'"}')
        check("oversized body -> 413", st, 413)
        hdr = {"Content-Type": "application/json", "X-Requested-With": "dashboard"}
        big = json.dumps({"config": {"all": 30, "failures": None, "successful": None}, "pad": "x" * 20000}).encode()
        check("handler core also caps the body (not only the socket wrapper)", m._lr_handle("POST", "/api/log-retention", hdr, big)[0], 413)
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 12, "failures": None, "successful": None}}, headers={"X-Requested-With": "other"})
        check("missing CSRF header -> 403", st, 403)
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 12, "failures": None, "successful": None}}, headers={"Content-Type": "text/plain"})
        check("non-JSON content-type -> 415", st, 415)
        st, d = http(port, "DELETE", "/api/log-retention/pin?id=abc", headers={"X-Requested-With": "other"})
        check("DELETE without CSRF header -> 403", st, 403)
        check("rejects wrote nothing", cfgp.exists(), False)

        # --- save round trip
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 45, "failures": 120, "successful": 7}})
        check("save ok", (st, d["ok"], d["config"]["all"], d["config"]["failures"], d["config"]["successful"]), (200, True, 45, 120, 7))
        on_disk = json.loads(cfgp.read_text())
        check("file holds the saved levels", (on_disk["all"], on_disk["failures"], on_disk["successful"], on_disk["ledger_days"]), (45, 120, 7, 365))
        st, d = http(port, "GET", "/api/log-retention")
        check("GET reflects the save", (d["config"]["all"], d["config"]["failures"], d["config"]["successful"]), (45, 120, 7))
        check("save audited by dashboard", "config-saved by=dashboard" in audit.read_text(), True)
        before = cfgp.read_text()
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 500, "failures": None, "successful": None}})
        check("invalid save rejected with reason", (st, d["ok"], "1..365" in d["error"]), (400, False, True))
        check("invalid save wrote nothing (no partial state)", cfgp.read_text(), before)
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 10, "failures": 0, "successful": None}})
        check("failures=0 rejected on save", st, 400)
        check("still nothing written", cfgp.read_text(), before)
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 30, "failures": None, "successful": None}})
        check("clearing overrides returns them to inherit", (st, d["config"]["failures"], d["config"]["successful"]), (200, None, None))

        # --- pins
        st, d = http(port, "POST", "/api/log-retention/pin", {"job_or_bundle": "0160287d360a", "reason": "debugging X"})
        pin = d["config"]["pins"][0] if st == 200 else {}
        check("pin job ok", (st, pin.get("type"), pin.get("id"), pin.get("who"), pin.get("why")), (200, "job", "0160287d360a", "dashboard", "debugging X"))
        check("pin has a UTC when", bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", pin.get("when", ""))), True)
        st, d = http(port, "POST", "/api/log-retention/pin", {"job_or_bundle": "smoke-egift-s4", "reason": "keep"})
        check("pin bundle ok", (st, d["config"]["pins"][1]["type"]), (200, "bundle"))
        st, d = http(port, "POST", "/api/log-retention", {"config": {"all": 60, "failures": None, "successful": None}})
        check("saving levels preserves pins", ([p["id"] for p in d["config"]["pins"]], d["config"]["all"]), (["0160287d360a", "smoke-egift-s4"], 60))
        st, d = http(port, "POST", "/api/log-retention/pin", {"job_or_bundle": "0160287d360a", "reason": "again"})
        check("duplicate pin -> 409", st, 409)
        for name, body in [("path traversal", {"job_or_bundle": "../../etc/passwd", "reason": "x"}),
                           ("slash", {"job_or_bundle": "a/b", "reason": "x"}),
                           ("space", {"job_or_bundle": "a b", "reason": "x"}),
                           ("empty id", {"job_or_bundle": "", "reason": "x"}),
                           ("long id", {"job_or_bundle": "a" * 81, "reason": "x"}),
                           ("id not string", {"job_or_bundle": 12, "reason": "x"}),
                           ("empty reason", {"job_or_bundle": "okid", "reason": "  "}),
                           ("reason too long", {"job_or_bundle": "okid", "reason": "r" * 201}),
                           ("reason not string", {"job_or_bundle": "okid", "reason": ["a"]}),
                           ("missing reason", {"job_or_bundle": "okid"}),
                           ("client-supplied who", {"job_or_bundle": "okid", "reason": "x", "who": "root"}),
                           ("client-supplied type", {"job_or_bundle": "okid", "reason": "x", "type": "job"})]:
            st, d = http(port, "POST", "/api/log-retention/pin", body)
            check(f"pin rejects {name}", (st, d and d.get("ok")), (400, False))
        check("rejected pins wrote nothing", len(json.loads(cfgp.read_text())["pins"]), 2)
        st, d = http(port, "DELETE", "/api/log-retention/pin?id=0160287d360a")
        check("unpin ok", (st, [p["id"] for p in d["config"]["pins"]]), (200, ["smoke-egift-s4"]))
        st, d = http(port, "DELETE", "/api/log-retention/pin?id=0160287d360a")
        check("unpin of absent id -> 404", st, 404)
        for q in ("", "?id=", "?id=../x", "?id=a&id=b", "?x=1"):
            st, d = http(port, "DELETE", "/api/log-retention/pin" + q)
            check(f"unpin rejects query {q!r}", st, 400)
        check("unpin left the other pin", [p["id"] for p in json.loads(cfgp.read_text())["pins"]], ["smoke-egift-s4"])
        st, d = http(port, "GET", "/api/log-retention")
        check("preview sees pins from the file", calls[-1]["pins"][0]["id"], "smoke-egift-s4")
        check("unknown sub-route 404", http(port, "POST", "/api/log-retention/nope", {})[0], 404)
        check("GET on pin route 404", http(port, "GET", "/api/log-retention/pin")[0], 404)

        # --- audit summary parsing
        audit.write_text("2026-10-13T08:00:00Z rotate mode=apply deleted=10 freed_bytes=2048 protected=1 pinned=0 skipped[x=1]\n"
                         "2026-10-13T09:00:00Z config-saved by=dashboard all: 30->45\n"
                         "2026-10-13T10:00:00Z rotate mode=dry-run deleted=99 freed_bytes=9999 protected=1 pinned=0 skipped[x=1]\n")
        st, d = http(port, "GET", "/api/log-retention")
        lr = d["last_rotation"]
        check("last_run is the newest rotate line", (lr["last_run"]["ts"], lr["last_run"]["mode"], lr["last_run"]["freed_bytes"]), ("2026-10-13T10:00:00Z", "dry-run", 9999))
        check("last_apply is the newest APPLY run", (lr["last_apply"]["ts"], lr["last_apply"]["deleted"], lr["last_apply"]["freed_bytes"]), ("2026-10-13T08:00:00Z", 10, 2048))
        check("last config save surfaced", lr["last_config_saved"]["ts"], "2026-10-13T09:00:00Z")
        audit.unlink()
        check("no audit log -> empty summary, no crash", m._lr_audit_summary()["last_run"], None)

        # --- the real dry-run (temp HOME has no queue state): honest 503, never a fake zero
        C.preview = real_preview
        m._lr_preview_cache.clear()
        st, d = http(port, "POST", "/api/log-retention/preview", {"config": {"all": 30, "failures": None, "successful": None}})
        check("unavailable dry run is a 503 with a reason", (st, d["ok"], bool(d["error"])), (503, False, True))
    finally:
        C.preview = real_preview
        srv.shutdown()
        srv.server_close()


def run(src):
    m, home = load_module(src)
    html = m.FRONTEND_HTML
    check("panel is collapsed by default", '<details id="logRetention">' in html, True)
    check("panel states the 04:17 schedule", "<strong>daily at 04:17</strong>" in html, True)
    check("save + unpin handlers wired", ("lrEl('lrSave').addEventListener('click', lrSave);" in html
                                          and "if (b) lrUnpin(b.getAttribute('data-id'));" in html), True)
    check("panel refreshes only while open", "if (lrEl('logRetention').open) refreshLogRetention(); }, 60000)" in html, True)
    js_checks(html)
    api_checks(m, home)


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else DEFAULT_SRC
    if "--mutants" in sys.argv:
        text = src.read_text()
        bad = 0
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                print(f"MUTANT-STALE: {name} (anchor count {text.count(old)})"); bad += 1; continue
            td = Path(tempfile.mkdtemp())
            for sib in src.parent.glob("*.py"):
                if sib != src:
                    (td / sib.name).write_text(sib.read_text())
            tmp = td / "m.py"
            tmp.write_text(text.replace(old, new))
            r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True,
                               env={**os.environ, "HOME": REAL_HOME})
            killed = r.returncode != 0
            print(("killed  " if killed else "SURVIVED") + f": {name}")
            bad += (not killed)
        print("MUTANTS OK" if not bad else f"{bad} mutant problem(s)")
        sys.exit(1 if bad else 0)
    run(src)
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


main()
