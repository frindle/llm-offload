#!/usr/bin/env python3
"""The yellow 'Waiting on' banner must never keep presenting a stale line as current (the owner
2026-10-12: it sat on 'STUDIO-DB IDLE 1M Waiting on: INFRA WAIT ...' after the condition cleared).
Old bugs: refresh() awaited the slowest fetch before repainting; a failed / non-ok fetch fell into
`keep last`; the idle age came from the frozen payload's own `now`.
Runs the page's renderWaitBanner / refreshWaitBanner under node against a fake DOM + fetch:
  * payload with no idle-with-pending lane clears (hides) a previously shown banner;
  * fetch rejection, non-ok response, and a HANGING fetch (aborted at the timeout) all replace the
    old banner with 'status unavailable (last seen Ns ago)', never the old 'Waiting on' text;
  * idle age advances with the client clock after fetch (a frozen payload visibly ages);
  * wiring: the banner has its own timer and is no longer inside refresh()'s Promise.all.
Run: python3 tests/test-dashboard-wait-banner.py [--src PATH] [--mutants]"""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []
DEFAULT_SRC = HERE.parent / "src" / "ollama-queue-api.py"

MUTANTS = [
    ("failure keeps last banner", "    queueWait = null;\n    renderWaitBannerUnavailable();", "    /* keep last */"),
    ("non-ok keeps last banner", "if (!r || !r.ok) throw new Error('queue-wait not ok');", "if (!r || !r.ok) return;"),
    ("no abort timeout", "setTimeout(() => ac.abort(), WAIT_BANNER_TIMEOUT_MS)", "setTimeout(() => {}, WAIT_BANNER_TIMEOUT_MS)"),
    ("empty payload does not clear", "el.hidden = !lines.length;", "if (lines.length) el.hidden = false;"),
    ("age frozen on payload clock", "+ (fetchedAt ? (Date.now() - fetchedAt) / 1000 : 0);", "+ 0;"),
    ("banner back inside refresh() batch", "setInterval(refreshWaitBanner, 4000);", ""),
    ("unsat warnings not grouped", "const key = (u.kind === 'capacity' ? 'capacity' : 'unsat') + '|' + String(u.model || '') + '|'", "const key = String(u.id) + '|'"),
    ("group key keeps drifting numbers", "reason.replace(/\\d+(?:\\.\\d+)?\\s*(?:GB|s|m|h)\\b/g, '#')", "reason"),
    ("group count dropped", "const n = g.ids.length, jobs = n + (n === 1 ? ' job' : ' jobs');", "const n = 1, jobs = 'job';"),
    ("group loses job ids tooltip", "g.ids.join(', ') + ' -- ' + g.reason", "g.reason"),
    ("lane chips never rendered", "  renderLaneChips(w, now);\n  el.hidden", "  el.hidden"),
    ("chip lacks full-sentence hover", 'title="${escapeHtml(full)}"', 'title=""'),
    ("chip lacks pending count", "(${ln.pending} pending)</span>`);", "</span>`);"),
    ("chip age frozen", "idle ${fmtDur(now - (ln.idle_since || now))}, `", "idle 0s, `"),
    ("unavailable leaves chips", "  renderLaneChips(null, 0);\n  el.innerHTML = '<span class=\"lane log\">Queue wait", "  el.innerHTML = '<span class=\"lane log\">Queue wait"),
    ("banner repeats per-lane sentence", "// Idle lanes live in the lane-summary chips; the banner only adds a short red line for a stuck lane.\n    for (const lane of (w.stuck || [])) {", "for (const lane of Object.keys(w.lanes || {})) {"),
    ("unavailable hides age", "' (last seen ' + Math.max(0, Math.round((Date.now() - waitBannerOkAt) / 1000)) + 's ago)'", "''"),
]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def html_of(src):
    home = tempfile.mkdtemp(prefix="dash-wb-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_wb", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_wb", ld))
    ld.exec_module(m)
    return m.FRONTEND_HTML


def grab(html, name):
    m = re.search(r"^(?:async )?function " + name + r"\(.*?\n}\n", html, re.S | re.M)
    return m.group(0) if m else None


JS = r"""
const el = {hidden: true, innerHTML: '', classList: {toggle(){}, remove(){}, add(){}}};
const chips = {hidden: true, innerHTML: ''};
globalThis.document = {getElementById: id => id === 'waitBanner' ? el : id === 'laneChips' ? chips : null};
let queueWait = null;
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mode = 'ok', payload = null;
globalThis.fetch = (url, opts) => {
  if (mode === 'reject') return Promise.reject(new Error('net'));
  if (mode === 'notok') return Promise.resolve({ok: false, status: 502, json: async () => ({})});
  if (mode === 'hang') return new Promise((_, rej) => opts.signal.addEventListener('abort', () => rej(new Error('abort'))));
  return Promise.resolve({ok: true, json: async () => payload});
};
%PARTS%
WAIT_BANNER_TIMEOUT_MS = 50;
const idle = now => ({source: 'state', now, stuck: [], lanes: {'studio-db': {state: 'idle', pending: 1, idle_since: now - 60,
  reason: {sentence: 'INFRA WAIT: bloom-control switching Darkbloom', since: now - 60}}}});
const busy = now => ({source: 'state', now, stuck: [], lanes: {'studio-db': {state: 'busy', running: ['x']}}});
(async () => {
  const out = {};
  const T = Date.now() / 1000;
  payload = idle(T); await refreshWaitBanner();
  out.shown = !chips.hidden && chips.innerHTML.includes('INFRA WAIT: bloom-control switching Darkbloom');
  out.banner_quiet = el.hidden === true && !el.innerHTML.includes('INFRA WAIT');
  out.age1 = /studio-db: idle 1m/.test(chips.innerHTML) && /\(1 pending\)/.test(chips.innerHTML);
  payload = idle(T); payload.unsat = [{id: 'u1', label: 'u1', model: 'm', age_s: 70, reason: 'needs 9.0 GB, have 4.0 GB usable (5.0 GB short)'}]; await refreshWaitBanner();
  out.warn_shown = !el.hidden && el.innerHTML.includes('1 job waiting');
  payload = busy(T); await refreshWaitBanner();
  out.cleared = el.hidden === true && !el.innerHTML.includes('Waiting on') && chips.hidden === true && chips.innerHTML === '';
  for (const m of ['reject', 'notok', 'hang']) {
    mode = 'ok'; payload = idle(T); await refreshWaitBanner();
    mode = m; await refreshWaitBanner();
    out[m + '_chips_cleared'] = chips.hidden === true && chips.innerHTML === '';
    out[m] = !el.innerHTML.includes('Waiting on') && !el.innerHTML.includes('INFRA WAIT') && /unavailable \(last seen \d+s ago\)/.test(el.innerHTML) && !el.hidden;
    out[m + '_busy_cleared'] = waitBannerBusy === false;
  }
  // age advances with the client clock after fetch
  const realNow = Date.now; const w = idle(T);
  Date.now = () => realNow() + 150000;
  renderWaitBanner(w, [], realNow());
  out.age_advances = /idle 3m/.test(chips.innerHTML);
  Date.now = realNow;
  // screenshot case: 25 pending studio-db + 9 identical unsat warnings with drifting numbers
  const sentence = 'darkbloom cannot load qwen3.6-35b-a3b-vl-mtp-mxfp8 blocked by Qwen3.5-9B (serving, last active 230s ago): needs 30.3 GB, load_usable 24.3 GB';
  const big = idle(T); big.lanes['studio-db'].pending = 25; big.lanes['studio-db'].idle_since = T - 960; big.lanes['studio-db'].reason.sentence = sentence;
  big.unsat = [];
  for (let i = 0; i < 9; i++) big.unsat.push({id: 'id' + i, label: 'psc-C' + i + '-a465e147cf00', model: 'qwen3.6-35b', age_s: 900 + i * 7,
    reason: `darkbloom cannot load X blocked by Qwen3.5-9B (serving, last active ${200 + i * 3}s ago): needs 30.3 GB, have ${23.3 + (i % 2) * 0.1} GB usable (${(7.0 - (i % 2) * 0.1).toFixed(1)} GB short)`});
  big.unsat.push({id: 'other', label: 'other-job', model: 'gemma', age_s: 5, reason: 'darkbloom cannot load Y: needs 9.0 GB, have 4.0 GB usable (5.0 GB short)'});
  renderWaitBanner(big, [], Date.now());
  out.big_warn_lines = (el.innerHTML.match(/class="badge">WARNING/g) || []).length;
  out.big_group9 = /9 jobs waiting, no host can serve: <b>qwen3.6-35b<\/b> needs 30.3 GB, 23\.\d GB usable \(\d\.\d GB short\), oldest 15m/.test(el.innerHTML);
  out.big_single = /1 job waiting, no host can serve: <b>gemma/.test(el.innerHTML);
  out.big_ids = /title="[^"]*psc-C0-a465e147cf00[^"]*psc-C8-a465e147cf00/.test(el.innerHTML);
  out.big_chip = /studio-db: idle 16m, waiting on memory\/model load \(25 pending\)/.test(chips.innerHTML) && chips.innerHTML.includes('title="darkbloom cannot load');
  out.big_banner_no_sentence = !el.innerHTML.replace(/title="[^"]*"/g, '').includes('Qwen3.5-9B (serving') && !el.innerHTML.includes('cannot load qwen3.6-35b-a3b-vl');
  const st = idle(T); st.stuck = ['studio-db'];
  renderWaitBanner(st, [], Date.now());
  out.stuck_line = /POSSIBLE STUCK SEAM/.test(el.innerHTML) && /lchip stuck/.test(chips.innerHTML);
  console.log(JSON.stringify(out));
})();
"""


def run(src):
    html = html_of(src)
    names = ["renderWaitBanner", "renderWaitBannerUnavailable", "refreshWaitBanner", "unsatBannerLines", "fmtDur", "laneChipReason", "renderLaneChips"]
    parts = {n: grab(html, n) for n in names}
    for n, p in parts.items():
        check(f"{n} present", bool(p), True)
    if not all(parts.values()):
        return
    state = re.search(r"^let waitBannerJobs.*$", html, re.M)
    check("banner state declared", bool(state), True)
    if not state:
        return
    js = JS.replace("%PARTS%", state.group(0) + "\n" + "\n".join(parts.values()))
    f = Path(tempfile.mkdtemp()) / "t.js"
    f.write_text(js)
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        check("node harness ran to completion (a hung fetch never aborted would stall it)", r.stdout[-200:] + r.stderr[-300:], "completed")
        return
    check("idle-with-pending lane shows in the lane-summary chip (full sentence in title)", o["shown"], True)
    check("unsat warning shows in the banner", o["warn_shown"], True)
    check("banner itself stays hidden for a plain idle lane", o["banner_quiet"], True)
    check("screenshot case: 10 unsat warnings collapse to 2 lines", o["big_warn_lines"], 2)
    check("identical unsat warnings grouped as 'N jobs waiting: model needs..., oldest'", o["big_group9"], True)
    check("a different model/reason stays its own line", o["big_single"], True)
    check("grouped line carries all job ids in its title", o["big_ids"], True)
    check("lane chip: 'studio-db: idle 16m, waiting on memory/model load (25 pending)'", o["big_chip"], True)
    check("banner no longer carries the long lane sentence", o["big_banner_no_sentence"], True)
    check("stuck lane: short red banner line + stuck chip", o["stuck_line"], True)
    check("chip shows 'studio-db: idle 1m' and pending count", o["age1"], True)
    check("no idle-with-pending lane clears the banner", o["cleared"], True)
    for m in ("reject", "notok", "hang"):
        check(f"fetch {m}: stale banner replaced by 'unavailable (last seen Ns ago)'", o[m], True)
        check(f"fetch {m}: stale lane chips cleared", o[m + "_chips_cleared"], True)
        check(f"fetch {m}: in-flight guard released", o[m + "_busy_cleared"], True)
    check("idle age advances with the client clock", o["age_advances"], True)
    check("banner has its own 4s timer", "setInterval(refreshWaitBanner, 4000);" in html, True)
    check("banner fetch no longer in refresh() batch", re.search(r"Promise\.all\(\[fetch\('/api/jobs'\)(?:(?!\]\);).)*queue-wait", html, re.S) is None, True)
    check("banner fetch aborts on a timeout", "setTimeout(() => ac.abort(), WAIT_BANNER_TIMEOUT_MS)" in html, True)


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
            r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True)
            killed = r.returncode != 0
            print(("killed  " if killed else "SURVIVED") + f": {name}")
            bad += (not killed)
        print("MUTANTS OK" if not bad else f"{bad} mutant problem(s)")
        sys.exit(1 if bad else 0)
    run(src)
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


main()
